"""
Bulk Create module — handles folder upload, PDF classification, insured grouping,
and batch submission creation.

Flow:
1. POST /api/bulk-upload — receives multiple PDFs, classifies each (app vs quote),
   extracts insured names, groups them, returns preview for user confirmation.
2. POST /api/bulk-create — takes confirmed groupings, creates submissions,
   parses apps + quotes, advances matched ones to Quoting stage.
"""

import os
import json
import time
import gc
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import current_app, session

import pdfplumber

import settings
from app.parsers.llm_parsers import BedrockClient, GroqClient
from app.parsers.two_pass_parser import groq_request_with_backoff, _is_text_garbage
from app.parsers.application_parser import process_application_two_pass
from app.parsers.two_pass_parser import process_quote_two_pass
from app.database import create_submission, create_quote, get_session, log_action
from app.models import (
    Submission, Quote, Document, DocumentType, SubmissionStatus, QuoteStatus, AuditLog
)


# ============================================================================
# PDF CLASSIFICATION
# ============================================================================

# Strong text signals for classification without LLM
APPLICATION_SIGNALS = [
    'ACORD', 'APPLICANT', 'NAMED INSURED', 'FIRST NAMED INSURED',
    'APPLICATION FOR', 'AGENCY NAME', 'POLICY PERIOD',
    'ACORD 125', 'ACORD 126', 'ACORD 130', 'ACORD 131', 'ACORD 140',
    'COMMERCIAL INSURANCE APPLICATION', 'GENERAL LIABILITY SECTION',
    'SECTION I', 'SECTION II', 'SECTION III',
]

QUOTE_SIGNALS = [
    'QUOTE', 'INDICATION', 'PROPOSAL', 'BINDING AUTHORITY',
    'SURPLUS LINES', 'BROKERAGE',
    'MINIMUM EARNED', 'POLICY FEE', 'INSPECTION FEE', 'BROKER FEE',
    'UNDERWRITER', 'LLOYD\'S', 'QUOTE NUMBER', 'INDICATION NUMBER',
    'SUBJECT TO', 'SUBJECTIVITIES', 'PRIOR TO BINDING',
    'WE ARE PLEASED TO OFFER', 'COVERAGE OFFERED', 'PREMIUM INDICATION',
]

# Supporting document signals — these are NOT quotes and should not be parsed as such
LOSS_RUN_SIGNALS = [
    'LOSS RUN', 'LOSS HISTORY', 'CLAIMS HISTORY', 'CLAIM #',
    'NO LOSSES REPORTED', '5 YEAR HISTORY', '3 YEAR HISTORY',
    'DATE OF LOSS', 'LOSS SUMMARY', 'CLAIMS SUMMARY',
]

SOV_SIGNALS = [
    'STATEMENT OF VALUE', 'SCHEDULE OF VALUES', 'SOV',
    'REPLACEMENT VALUE', 'REPLACEMENT COST', 'BUILDING VALUE',
    'INSURED VALUES', 'PROPERTY SCHEDULE', 'LOCATION SCHEDULE',
]

BINDER_SIGNALS = [
    'INSURANCE BINDER', 'BINDER NUMBER', 'EVIDENCE OF INSURANCE',
    'TEMPORARY EVIDENCE', 'BINDER DETAILS', 'COVERAGE IS IN FORCE',
    'CERTIFICATE OF INSURANCE', 'CERTIFICATE HOLDER',
]

FINANCE_SIGNALS = [
    'PREMIUM FINANCE AGREEMENT', 'FINANCE AGREEMENT',
    'AMOUNT TO FINANCE', 'INSTALLMENT AMOUNT', 'PAYMENT SCHEDULE',
    'DOWN PAYMENT', 'ANNUAL PERCENTAGE RATE', 'FINANCE CHARGE',
    'NUMBER OF INSTALLMENTS',
]


def _classify_from_text(text: str) -> dict:
    """
    Classify a PDF from extracted text using keyword signals.
    Returns {'type': 'application'|'quote'|'supporting_doc', 'confidence': float,
             'insured_name_hint': str|None, 'doc_subtype': str|None}
    """
    upper = text.upper()

    # Score everything first
    app_score = sum(1 for signal in APPLICATION_SIGNALS if signal in upper)
    quote_score = sum(1 for signal in QUOTE_SIGNALS if signal in upper)
    loss_run_score = sum(1 for signal in LOSS_RUN_SIGNALS if signal in upper)
    sov_score = sum(1 for signal in SOV_SIGNALS if signal in upper)
    binder_score = sum(1 for signal in BINDER_SIGNALS if signal in upper)
    finance_score = sum(1 for signal in FINANCE_SIGNALS if signal in upper)

    # Strong ACORD presence is very reliable — check first
    if 'ACORD' in upper and app_score >= 3:
        return {'type': 'application', 'confidence': 0.95, 'insured_name_hint': None, 'doc_subtype': None}

    # Find the highest supporting doc score
    sup_scores = {
        'loss_run': loss_run_score,
        'sov': sov_score,
        'binder': binder_score,
        'finance_agreement': finance_score,
    }
    best_sup_type = max(sup_scores, key=sup_scores.get)
    best_sup_score = sup_scores[best_sup_type]

    # If quote signals are strong (>=3) AND higher than any supporting doc type, it's a quote.
    # Quotes often contain finance summaries, premium breakdowns, etc.
    if quote_score >= 3 and quote_score > best_sup_score:
        return {'type': 'quote', 'confidence': 0.85, 'insured_name_hint': None, 'doc_subtype': None}

    # Check supporting doc types — classify if their signals are strong and outweigh quote signals
    if best_sup_score >= 2 and best_sup_score >= quote_score:
        return {'type': 'supporting_doc', 'confidence': 0.9, 'insured_name_hint': None, 'doc_subtype': best_sup_type}

    # Standard app vs quote decision
    if quote_score >= 3 and app_score < 2:
        return {'type': 'quote', 'confidence': 0.85, 'insured_name_hint': None, 'doc_subtype': None}

    if app_score >= 2 and quote_score < 2:
        return {'type': 'application', 'confidence': 0.75, 'insured_name_hint': None, 'doc_subtype': None}

    if quote_score >= 2:
        return {'type': 'quote', 'confidence': 0.7, 'insured_name_hint': None, 'doc_subtype': None}

    return {'type': 'unknown', 'confidence': 0.0, 'insured_name_hint': None, 'doc_subtype': None}


def _get_classification_llm():
    """Get a cheap/fast LLM client for classification tasks."""
    provider = settings.LLM_PROVIDER.lower()
    if provider == 'groq':
        if settings.GROQ_API_KEY:
            return GroqClient(settings.GROQ_API_KEY, model=settings.GROQ_MODEL)
    # Fall back to Bedrock Haiku (cheap)
    return BedrockClient(model="us.anthropic.claude-haiku-4-5-20251001-v1:0", region=settings.BEDROCK_REGION)


CLASSIFY_PROMPT = """You are classifying an insurance PDF document. Based on the content below, determine:
1. Document type:
   - "application" = ACORD form, submission form, insurance application
   - "quote" = premium indication, quote proposal, coverage offer from a carrier/underwriter
   - "supporting_doc" = loss run, statement of values, binder, finance agreement, certificate, schedule
   - "other" = doesn't fit any category
2. What is the insured's name?

Return ONLY valid JSON:
{
  "type": "application" or "quote" or "supporting_doc" or "other",
  "insured_name": "string or null",
  "confidence": 0.0 to 1.0
}

Document content (first 2 pages):
"""

CLASSIFY_VISION_PROMPT = """You are looking at the first page of an insurance PDF document. Determine:
1. Document type:
   - "application" = ACORD form, submission form, insurance application
   - "quote" = premium indication, quote proposal, coverage offer from a carrier/underwriter
   - "supporting_doc" = loss run, statement of values, binder, finance agreement, certificate, schedule
   - "other" = doesn't fit any category
2. What is the insured's name?

Return ONLY valid JSON:
{
  "type": "application" or "quote" or "supporting_doc" or "other",
  "insured_name": "string or null",
  "confidence": 0.0 to 1.0
}"""


def classify_pdf(pdf_path: str) -> dict:
    """
    Classify a single PDF as application, quote, supporting_doc, or other.
    Uses text extraction first (fast), falls back to vision for scanned PDFs.
    Also extracts the insured name for grouping.
    
    Returns:
        {
            'type': 'application' | 'quote' | 'supporting_doc' | 'other',
            'insured_name': str | None,
            'confidence': float,
            'method': 'text_signals' | 'text_llm' | 'vision_llm',
            'doc_subtype': str | None,  (loss_run, sov, binder, finance_agreement)
            'filename': str
        }
    """
    filename = os.path.basename(pdf_path)
    result = {
        'type': 'other',
        'insured_name': None,
        'confidence': 0.0,
        'method': 'unknown',
        'doc_subtype': None,
        'filename': filename,
        'filepath': pdf_path
    }

    try:
        with pdfplumber.open(pdf_path) as pdf:
            if not pdf.pages:
                return result

            # Extract text from first 2 pages
            text_content = ''
            for i, page in enumerate(pdf.pages[:2]):
                page_text = page.extract_text() or ''
                text_content += page_text + '\n'

            is_scanned = not text_content.strip() or _is_text_garbage(text_content)

            if not is_scanned:
                # Try keyword classification first (no LLM cost)
                signal_result = _classify_from_text(text_content)
                if signal_result['confidence'] >= 0.75:
                    # Good enough from signals alone — still need insured name from LLM
                    result['type'] = signal_result['type']
                    result['confidence'] = signal_result['confidence']
                    result['doc_subtype'] = signal_result.get('doc_subtype')
                    result['method'] = 'text_signals'

                    # Quick LLM call just for insured name extraction
                    try:
                        llm = _get_classification_llm()
                        # Truncate text to keep prompt small
                        truncated = text_content[:3000]
                        name_prompt = (
                            "Extract ONLY the insured/applicant name from this insurance document text. "
                            "Return ONLY valid JSON: {\"insured_name\": \"string or null\"}\n\n"
                            f"Text:\n{truncated}"
                        )
                        name_result = groq_request_with_backoff(lambda: llm.generate_json(name_prompt))
                        result['insured_name'] = name_result.get('insured_name')
                    except Exception as e:
                        print(f"  [Bulk] Name extraction failed for {filename}: {e}")
                        # Try regex fallback for insured name
                        result['insured_name'] = _extract_insured_name_regex(text_content)

                    return result

                # Signals inconclusive — use LLM for full classification
                try:
                    llm = _get_classification_llm()
                    truncated = text_content[:4000]
                    prompt = CLASSIFY_PROMPT + truncated
                    llm_result = groq_request_with_backoff(lambda: llm.generate_json(prompt))
                    result['type'] = llm_result.get('type', 'other')
                    result['insured_name'] = llm_result.get('insured_name')
                    result['confidence'] = llm_result.get('confidence', 0.7)
                    result['method'] = 'text_llm'
                except Exception as e:
                    print(f"  [Bulk] LLM classification failed for {filename}: {e}")
                    # Fall through with signal-based result if any
                    if signal_result['type'] != 'unknown':
                        result['type'] = signal_result['type']
                        result['confidence'] = signal_result['confidence']
                        result['doc_subtype'] = signal_result.get('doc_subtype')
                        result['method'] = 'text_signals_fallback'
                    # Try regex for name regardless
                    result['insured_name'] = _extract_insured_name_regex(text_content)

                return result

            else:
                # Scanned PDF — need vision
                try:
                    first_page = pdf.pages[0]
                    page_image = first_page.to_image(resolution=150).original
                    # Resize for efficiency
                    if page_image.width > 1000:
                        ratio = 1000 / page_image.width
                        new_size = (1000, int(page_image.height * ratio))
                        from PIL import Image
                        page_image = page_image.resize(new_size, Image.LANCZOS)

                    llm = _get_classification_llm()
                    vision_result = groq_request_with_backoff(
                        lambda: llm.generate_json_with_images(CLASSIFY_VISION_PROMPT, [page_image])
                    )
                    result['type'] = vision_result.get('type', 'other')
                    result['insured_name'] = vision_result.get('insured_name')
                    result['confidence'] = vision_result.get('confidence', 0.7)
                    result['method'] = 'vision_llm'
                except Exception as e:
                    print(f"  [Bulk] Vision classification failed for {filename}: {e}")

                return result

    except Exception as e:
        print(f"  [Bulk] Failed to open/process {filename}: {e}")
        result['method'] = 'error'
        return result


def _extract_insured_name_regex(text: str) -> str:
    """
    Fallback: extract insured name from text using common patterns.
    Looks for patterns like 'Named Insured: Foo Bar' or 'Insured: Foo Bar'.
    """
    import re
    # Common patterns for insured name in insurance docs
    patterns = [
        r'(?:Named\s+Insured|Insured|Applicant|First Named Insured)[:\s]*\n?\s*([A-Z][A-Za-z0-9\s,.\-&\'\/]+?)(?:\n|Quote|DBA|Eff|Address|Phone)',
        r'(?:Named\s*\n\s*)([A-Z][A-Za-z0-9\s,.\-&\'\/]+?)(?:\s*Quote|\s*DBA|\s*Eff)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            name = match.group(1).strip()
            # Clean up — remove trailing whitespace and common junk
            name = re.sub(r'\s+', ' ', name).strip()
            if len(name) > 3 and len(name) < 100:
                return name
    return None


# ============================================================================
# INSURED NAME GROUPING
# ============================================================================

def _normalize_name(name: str) -> str:
    """Normalize an insured name for fuzzy matching."""
    if not name:
        return ''
    import re
    # Lowercase, strip common suffixes, remove punctuation
    normalized = name.lower().strip()
    # Remove common business suffixes
    for suffix in [' llc', ' inc', ' inc.', ' corp', ' corp.', ' corporation',
                   ' ltd', ' ltd.', ' company', ' co', ' co.', ' llp', ' lp',
                   ' pllc', ' pc', ' p.c.', ' dba', ' d/b/a']:
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)].strip()
    # Remove punctuation and extra whitespace
    normalized = re.sub(r'[^\w\s]', '', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def _names_match(name1: str, name2: str) -> bool:
    """Check if two insured names likely refer to the same entity."""
    n1 = _normalize_name(name1)
    n2 = _normalize_name(name2)

    if not n1 or not n2:
        return False

    # Exact match after normalization
    if n1 == n2:
        return True

    # One contains the other (handles "ABC" matching "ABC Corporation")
    if n1 in n2 or n2 in n1:
        return True

    # Simple token overlap — if 80%+ of words match
    words1 = set(n1.split())
    words2 = set(n2.split())
    if not words1 or not words2:
        return False
    overlap = len(words1 & words2)
    max_words = max(len(words1), len(words2))
    if overlap / max_words >= 0.8:
        return True

    return False


def group_by_insured(classified_files: list) -> list:
    """
    Group classified PDFs by insured name.
    Returns a list of groups, each with:
    {
        'insured_name': str,
        'applications': [list of file dicts],
        'quotes': [list of file dicts],
        'supporting_docs': [list of file dicts],  (loss runs, SOVs, binders, etc.)
        'other': [list of file dicts],
        'stage': 'submission' | 'quoting'  (based on whether quotes exist)
    }
    """
    groups = []

    for item in classified_files:
        insured = item.get('insured_name')
        doc_type = item['type']

        if not insured:
            # Can't group without a name — put in its own group
            groups.append({
                'insured_name': item.get('filename', 'Unknown'),
                'applications': [item] if doc_type == 'application' else [],
                'quotes': [item] if doc_type == 'quote' else [],
                'supporting_docs': [item] if doc_type == 'supporting_doc' else [],
                'other': [item] if doc_type == 'other' else [],
            })
            continue

        # Try to find an existing group that matches
        matched_group = None
        for group in groups:
            if _names_match(group['insured_name'], insured):
                matched_group = group
                break

        if matched_group:
            if doc_type == 'application':
                matched_group['applications'].append(item)
            elif doc_type == 'quote':
                matched_group['quotes'].append(item)
            elif doc_type == 'supporting_doc':
                matched_group['supporting_docs'].append(item)
            else:
                matched_group['other'].append(item)
        else:
            # Create new group
            new_group = {
                'insured_name': insured,
                'applications': [],
                'quotes': [],
                'supporting_docs': [],
                'other': [],
            }
            if doc_type == 'application':
                new_group['applications'].append(item)
            elif doc_type == 'quote':
                new_group['quotes'].append(item)
            elif doc_type == 'supporting_doc':
                new_group['supporting_docs'].append(item)
            else:
                new_group['other'].append(item)
            groups.append(new_group)

    # Determine stage for each group
    for group in groups:
        has_binder = any(s.get('doc_subtype') == 'binder' for s in group['supporting_docs'])
        if has_binder:
            group['stage'] = 'bound'
        elif group['quotes']:
            group['stage'] = 'quoting'
        else:
            group['stage'] = 'submission'

    return groups


# ============================================================================
# BULK CREATE EXECUTION
# ============================================================================

def execute_bulk_create(groups: list, temp_dir: str) -> list:
    """
    Execute bulk creation for confirmed groups.
    
    For each group:
    1. Parse the application (if present) → create submission
    2. If quotes exist, parse each → create quote records → advance to Quoting stage
    3. Store documents in storage
    
    Args:
        groups: List of confirmed group dicts from the preview step
        temp_dir: Directory where uploaded files are stored
        
    Returns:
        List of result dicts with submission_id, status, errors
    """
    from app.short_name import generate_short_name
    from app.routes import _storage_upload, _build_storage_key

    results = []
    username = session.get('username')
    user_id = session.get('user_id')

    for group in groups:
        group_result = {
            'insured_name': group['insured_name'],
            'submission_id': None,
            'quote_ids': [],
            'stage': group.get('stage', 'submission'),
            'status': 'pending',
            'errors': []
        }

        try:
            insured_name = group['insured_name']
            effective_date = datetime.now().strftime('%Y-%m-%d')
            state = None
            intake_data = None

            # --- Parse application if present ---
            app_files = group.get('applications', [])
            app_filepath = None
            app_filename = None

            if app_files:
                app_info = app_files[0]  # Use first application
                app_filepath = app_info.get('filepath')
                app_filename = app_info.get('filename', 'application.pdf')

                if app_filepath and os.path.exists(app_filepath):
                    try:
                        application_result = process_application_two_pass(app_filepath)
                        parsed_data = application_result['pass2_normalized']

                        # Extract insured info from parsed app
                        parsed_name = (parsed_data.get('insured') or {}).get('name')
                        if parsed_name:
                            insured_name = parsed_name.strip()

                        state = (parsed_data.get('insured') or {}).get('address', {}).get('state')
                        submission_fields = parsed_data.get('submission') or {}
                        effective_date = submission_fields.get('effective_date') or effective_date

                        intake_data = {
                            'source': 'application',
                            'application_filename': app_filename,
                            'insured': parsed_data.get('insured'),
                            'retail_agent': parsed_data.get('retail_agent'),
                            'quote_number': parsed_data.get('quote_number'),
                            'account_number': parsed_data.get('account_number'),
                            'coverage_types': submission_fields.get('coverage_types_needed') or [],
                            'effective_date': effective_date,
                            'processing_metadata': application_result.get('processing_metadata', {})
                        }
                    except Exception as e:
                        group_result['errors'].append(f'App parse failed: {str(e)}')
                        print(f"  [Bulk] App parse failed for {app_filename}: {e}")
                        # Continue with manual intake
                        intake_data = {
                            'source': 'bulk_create_manual',
                            'insured': {'name': insured_name},
                            'coverage_types': [],
                            'effective_date': effective_date
                        }
            else:
                intake_data = {
                    'source': 'bulk_create_manual',
                    'insured': {'name': insured_name},
                    'coverage_types': [],
                    'effective_date': effective_date
                }

            # --- Create submission ---
            submission_id = create_submission(
                insured_name=insured_name,
                effective_date=effective_date,
                state=state,
                user=username,
                assigned_to=user_id
            )
            group_result['submission_id'] = submission_id

            # Store intake data and short name
            db_session = get_session()
            try:
                sub = db_session.query(Submission).filter_by(id=submission_id).first()
                if sub:
                    sub.submission_intake = json.dumps(intake_data)
                    sub.short_name = generate_short_name(insured_name)
                    db_session.commit()
            finally:
                db_session.close()

            # --- Upload application document ---
            if app_filepath and os.path.exists(app_filepath):
                try:
                    object_key = _build_storage_key(
                        submission_id, DocumentType.APPLICATION.name,
                        app_filename, user_id, insured_name
                    )
                    storage_provider, storage_key = _storage_upload(
                        app_filepath, object_key, 'application/pdf'
                    )
                    db_session = get_session()
                    try:
                        app_doc = Document(
                            submission_id=submission_id,
                            quote_id=None,
                            document_type=DocumentType.APPLICATION,
                            carrier=None,
                            term_key=effective_date,
                            version=1,
                            is_active=True,
                            storage_provider=storage_provider,
                            storage_key=storage_key,
                            original_filename=app_filename,
                            content_type='application/pdf',
                            size_bytes=os.path.getsize(app_filepath),
                            uploaded_by=username
                        )
                        db_session.add(app_doc)
                        db_session.commit()
                    finally:
                        db_session.close()
                except Exception as e:
                    group_result['errors'].append(f'App upload failed: {str(e)}')
                    print(f"  [Bulk] App document upload failed: {e}")

            # Log creation
            log_action(
                entity_type='submission',
                entity_id=submission_id,
                action='bulk_created',
                user=username,
                submission_id=submission_id,
                details=json.dumps({'source': 'bulk_create', 'intake': intake_data})
            )

            # --- Process quotes if present ---
            quote_files = group.get('quotes', [])
            if quote_files:
                for quote_info in quote_files:
                    quote_filepath = quote_info.get('filepath')
                    quote_filename = quote_info.get('filename', 'quote.pdf')

                    if not quote_filepath or not os.path.exists(quote_filepath):
                        group_result['errors'].append(f'Quote file not found: {quote_filename}')
                        continue

                    try:
                        # Parse quote
                        three_pass_result = process_quote_two_pass(quote_filepath)
                        parsed_data = three_pass_result['pass2_normalized']
                        layout_data = three_pass_result['pass1_layout']

                        # Extract carrier from first policy
                        carrier_name = None
                        if parsed_data.get('policies') and len(parsed_data['policies']) > 0:
                            carrier_name = parsed_data['policies'][0].get('carrier')

                        # Extract subjectivities
                        subjectivities = parsed_data.get('subjectivities')
                        subjectivities_json_str = json.dumps(subjectivities) if subjectivities else None

                        # Create quote record
                        quote_id = create_quote(
                            submission_id=submission_id,
                            carrier_name=carrier_name,
                            raw_document_path=quote_filepath,
                            extracted_json=json.dumps(parsed_data),
                            pass1_layout_json=json.dumps(layout_data),
                            subjectivities_json=subjectivities_json_str,
                            user=username
                        )
                        group_result['quote_ids'].append(quote_id)

                        # Upload quote document
                        try:
                            quote_doc_key = _build_storage_key(
                                submission_id, DocumentType.QUOTE.name,
                                quote_filename, user_id, insured_name
                            )
                            storage_provider, storage_key = _storage_upload(
                                quote_filepath, quote_doc_key, 'application/pdf'
                            )
                            db_session = get_session()
                            try:
                                doc = Document(
                                    submission_id=submission_id,
                                    quote_id=quote_id,
                                    document_type=DocumentType.QUOTE,
                                    carrier=carrier_name,
                                    term_key=effective_date,
                                    version=1,
                                    is_active=True,
                                    storage_provider=storage_provider,
                                    storage_key=storage_key,
                                    original_filename=quote_filename,
                                    content_type='application/pdf',
                                    size_bytes=os.path.getsize(quote_filepath),
                                    uploaded_by=username
                                )
                                db_session.add(doc)
                                db_session.commit()
                            finally:
                                db_session.close()
                        except Exception as e:
                            group_result['errors'].append(f'Quote doc upload failed: {str(e)}')

                    except Exception as e:
                        group_result['errors'].append(f'Quote parse failed ({quote_filename}): {str(e)}')
                        print(f"  [Bulk] Quote parse failed for {quote_filename}: {e}")

                # Advance to Quoting stage if quotes were successfully created
                if group_result['quote_ids']:
                    db_session = get_session()
                    try:
                        sub = db_session.query(Submission).filter_by(id=submission_id).first()
                        if sub and sub.status == SubmissionStatus.RECEIVED:
                            sub.status = SubmissionStatus.IN_PROGRESS
                            db_session.commit()
                            group_result['stage'] = 'quoting'
                    finally:
                        db_session.close()

            # --- Upload supporting docs (no parsing, just attach) ---
            supporting_files = group.get('supporting_docs', [])
            for sup_info in supporting_files:
                sup_filepath = sup_info.get('filepath')
                sup_filename = sup_info.get('filename', 'document.pdf')
                doc_subtype = sup_info.get('doc_subtype', '')

                if not sup_filepath or not os.path.exists(sup_filepath):
                    continue

                # Map subtype to DocumentType enum
                subtype_map = {
                    'loss_run': DocumentType.LOSS_RUN,
                    'sov': DocumentType.SOV,
                    'binder': DocumentType.BINDER,
                    'finance_agreement': DocumentType.FINANCE_AGREEMENT,
                }
                doc_type_enum = subtype_map.get(doc_subtype, DocumentType.OTHER)

                try:
                    doc_key = _build_storage_key(
                        submission_id, doc_type_enum.name,
                        sup_filename, user_id, insured_name
                    )
                    storage_provider, storage_key = _storage_upload(
                        sup_filepath, doc_key, 'application/pdf'
                    )
                    db_session = get_session()
                    try:
                        doc = Document(
                            submission_id=submission_id,
                            quote_id=None,
                            document_type=doc_type_enum,
                            carrier=None,
                            term_key=effective_date,
                            version=1,
                            is_active=True,
                            storage_provider=storage_provider,
                            storage_key=storage_key,
                            original_filename=sup_filename,
                            content_type='application/pdf',
                            size_bytes=os.path.getsize(sup_filepath),
                            uploaded_by=username
                        )
                        db_session.add(doc)
                        db_session.commit()
                    finally:
                        db_session.close()
                except Exception as e:
                    group_result['errors'].append(f'Supporting doc upload failed ({sup_filename}): {str(e)}')
                    print(f"  [Bulk] Supporting doc upload failed for {sup_filename}: {e}")

            # --- Advance to bound if a binder was imported ---
            has_binder = any(
                s.get('doc_subtype') == 'binder' for s in supporting_files
            )
            if has_binder:
                db_session = get_session()
                try:
                    sub = db_session.query(Submission).filter_by(id=submission_id).first()
                    if sub:
                        sub.status = SubmissionStatus.SENT_TO_FINANCE
                        db_session.commit()
                        group_result['stage'] = 'bound'
                finally:
                    db_session.close()

            group_result['status'] = 'created'

        except Exception as e:
            group_result['status'] = 'failed'
            group_result['errors'].append(str(e))
            print(f"  [Bulk] Group creation failed for {group.get('insured_name')}: {e}")

        results.append(group_result)

    # Cleanup temp files
    try:
        import shutil
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
    except Exception as e:
        print(f"  [Bulk] Temp dir cleanup failed: {e}")

    return results
