"""
Applied Epic integration routes.

Provides endpoints for:
- Searching Epic clients
- Importing client/policy data to create a RiskRunway submission
  (including downloading and parsing the ACORD 125 from Epic)
- Exporting bound submission data back to Epic
"""
from flask import Blueprint, request, jsonify, session, current_app
from datetime import datetime
from functools import wraps
import json
import os

from app.epic_client import get_epic_client, EpicAPIError
from app.database import get_session, create_submission, log_action
from app.models import Submission, SubmissionStatus, Document, DocumentType

epic_bp = Blueprint('epic', __name__)


def login_required(f):
    """Require authenticated session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'success': False, 'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated


@epic_bp.route('/api/epic/status', methods=['GET'])
@login_required
def epic_status():
    """Check if Epic integration is configured and accessible."""
    client = get_epic_client()
    return jsonify({
        'success': True,
        'configured': client.is_configured,
        'base_url': client.base_url,
    })


@epic_bp.route('/api/epic/clients/search', methods=['GET'])
@login_required
def epic_search_clients():
    """Search Epic clients by name."""
    client = get_epic_client()
    if not client.is_configured:
        return jsonify({'success': False, 'error': 'Epic integration not configured'}), 400

    query = (request.args.get('q') or '').strip()
    if not query or len(query) < 2:
        return jsonify({'success': False, 'error': 'Search query must be at least 2 characters'}), 400

    limit = min(int(request.args.get('limit', 20)), 100)

    try:
        clients = client.search_clients(name_contains=query, limit=limit)
        results = []
        for c in clients:
            results.append({
                'id': c.get('id'),
                'name': c.get('name'),
                'lookup_code': c.get('lookupCode'),
                'type': c.get('type'),
                'active': c.get('active'),
                'address': c.get('address'),
            })
        return jsonify({'success': True, 'clients': results})
    except EpicAPIError as e:
        return jsonify({'success': False, 'error': f'Epic API error: {e.detail}'}), e.status_code


@epic_bp.route('/api/epic/clients/<client_id>/policies', methods=['GET'])
@login_required
def epic_client_policies(client_id):
    """Get prospective policies for a given Epic client."""
    client = get_epic_client()
    if not client.is_configured:
        return jsonify({'success': False, 'error': 'Epic integration not configured'}), 400

    status_filter = request.args.get('status', 'PROSPECTIVE')

    try:
        policies = client.get_policies_for_client(client_id, status=status_filter)
        results = []
        for p in policies:
            embedded = p.get('_embedded', {})
            policy_type = embedded.get('policyType', {})
            results.append({
                'id': p.get('id'),
                'description': p.get('description'),
                'policy_number': p.get('policyNumber'),
                'effective_on': p.get('effectiveOn'),
                'expiration_on': p.get('expirationOn'),
                'status': p.get('status'),
                'policy_type_code': policy_type.get('code'),
                'policy_type_description': policy_type.get('description'),
                'business_type': policy_type.get('businessType'),
            })
        return jsonify({'success': True, 'policies': results})
    except EpicAPIError as e:
        return jsonify({'success': False, 'error': f'Epic API error: {e.detail}'}), e.status_code


@epic_bp.route('/api/epic/policies/<policy_id>/lines', methods=['GET'])
@login_required
def epic_policy_lines(policy_id):
    """Get lines for a specific Epic policy."""
    client = get_epic_client()
    if not client.is_configured:
        return jsonify({'success': False, 'error': 'Epic integration not configured'}), 400

    try:
        lines = client.get_lines_for_policy(policy_id)
        results = []
        for line in lines:
            embedded = line.get('_embedded', {})
            line_type = embedded.get('lineType', {})
            issuing_company = embedded.get('issuingCompany', {})
            results.append({
                'id': line.get('id'),
                'line_type_code': line_type.get('code'),
                'line_type_description': line_type.get('description'),
                'effective_on': line.get('effectiveOn'),
                'expiration_on': line.get('expirationOn'),
                'stage': line.get('stage'),
                'issuing_company': issuing_company.get('name'),
                'bill_mode': line.get('billMode'),
            })
        return jsonify({'success': True, 'lines': results})
    except EpicAPIError as e:
        return jsonify({'success': False, 'error': f'Epic API error: {e.detail}'}), e.status_code


@epic_bp.route('/api/epic/import', methods=['POST'])
@login_required
def epic_import_submission():
    """
    Import a client/policy from Epic to create a fully populated RiskRunway submission.

    Flow:
    1. Takes client/policy selection from the frontend
    2. Fetches system-generated ACORD 125 attachment from the policy
    3. Downloads the 125 PDF
    4. Parses it with the ACORD parser to extract insured/coverage data
    5. Creates a submission with all parsed fields populated
    6. Attaches the 125 PDF to the submission
    """
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Request body required'}), 400

    client_name = (data.get('client_name') or '').strip()
    if not client_name:
        return jsonify({'success': False, 'error': 'client_name is required'}), 400

    effective_date = data.get('effective_date') or datetime.now().strftime('%Y-%m-%d')
    state = data.get('state')
    policy_id = data.get('policy_id')

    # ── Step 1: Try to find and download the ACORD 125 from Epic ──
    epic_client = get_epic_client()
    acord_pdf_path = None
    parsed_application = None

    if policy_id and epic_client.is_configured:
        try:
            # Look for attachments on this policy
            attachments = epic_client.get_attachments_for_policy(
                policy_id, system_generated=True
            )

            # Find the ACORD 125 by description or extension
            acord_attachment = None
            for att in attachments:
                desc = (att.get('description') or '').lower()
                file_info = att.get('file', {})
                ext = (file_info.get('extension') or '').lower()
                if ('125' in desc or 'acord' in desc or 'application' in desc):
                    if ext in ('.pdf', 'pdf', '.PDF'):
                        acord_attachment = att
                        break

            # Fallback: take any PDF attachment with status OK
            if not acord_attachment and attachments:
                for att in attachments:
                    file_info = att.get('file', {})
                    ext = (file_info.get('extension') or '').lower()
                    status = (file_info.get('status') or '').upper()
                    if ext in ('.pdf', 'pdf', '.PDF') and status == 'OK' and file_info.get('url'):
                        acord_attachment = att
                        break

            # Download the PDF
            if acord_attachment:
                file_info = acord_attachment.get('file', {})
                file_url = file_info.get('url')
                if file_url:
                    pdf_bytes = epic_client.download_attachment_file(file_url)
                    if pdf_bytes and len(pdf_bytes) > 100:
                        # Save to uploads folder for parsing
                        upload_folder = current_app.config.get('UPLOAD_FOLDER', '/tmp')
                        os.makedirs(upload_folder, exist_ok=True)
                        safe_id = (policy_id or 'unknown')[:8]
                        filename = f"epic_125_{safe_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
                        acord_pdf_path = os.path.join(upload_folder, filename)
                        with open(acord_pdf_path, 'wb') as f:
                            f.write(pdf_bytes)

                        # Parse the ACORD 125
                        try:
                            from app.parsers.application_parser import process_application_two_pass
                            application_result = process_application_two_pass(acord_pdf_path)
                            parsed_application = application_result.get('pass2_normalized', {})
                            print(f"[EPIC IMPORT] Successfully parsed ACORD 125 ({len(pdf_bytes)} bytes)")
                        except Exception as parse_err:
                            print(f"[EPIC IMPORT] ACORD parse error: {parse_err}")
                            parsed_application = None

        except EpicAPIError as e:
            print(f"[EPIC IMPORT] Could not fetch attachments: {e}")
        except Exception as e:
            print(f"[EPIC IMPORT] Unexpected error fetching 125: {e}")

    # ── Step 2: Use parsed data to enrich submission fields ──
    if parsed_application:
        insured_data = parsed_application.get('insured') or {}
        parsed_name = (insured_data.get('name') or '').strip()
        if parsed_name:
            client_name = parsed_name
        parsed_state = (insured_data.get('address') or {}).get('state')
        if parsed_state:
            state = parsed_state
        submission_fields = parsed_application.get('submission') or {}
        if submission_fields.get('effective_date'):
            effective_date = submission_fields['effective_date']

    # ── Step 3: Create the submission ──
    submission_id = create_submission(
        insured_name=client_name,
        effective_date=effective_date,
        state=state,
        user=session.get('username'),
        assigned_to=session.get('user_id'),
    )

    # ── Step 4: Set Epic-specific fields ──
    db_session = get_session()
    try:
        submission = db_session.query(Submission).filter_by(id=submission_id).first()
        submission.ams_type = 'epic'
        submission.epic_client_id = data.get('client_id')
        submission.epic_policy_id = data.get('policy_id')
        submission.epic_line_id = data.get('line_id')
        # Set status label to policy type for kanban visibility
        policy_desc = data.get('policy_type_description') or data.get('line_type') or data.get('description') or ''
        if policy_desc:
            submission.status_label = policy_desc
        db_session.commit()
    finally:
        db_session.close()

    # ── Step 5: Attach the ACORD 125 PDF as a document ──
    if acord_pdf_path and os.path.exists(acord_pdf_path):
        try:
            file_size = os.path.getsize(acord_pdf_path)
            db_session = get_session()
            try:
                doc = Document(
                    submission_id=submission_id,
                    quote_id=None,
                    document_type=DocumentType.APPLICATION,
                    carrier=None,
                    term_key=effective_date,
                    version=1,
                    is_active=True,
                    storage_provider='local',
                    storage_key=acord_pdf_path,
                    original_filename=f"ACORD_125_{client_name.replace(' ', '_')}.pdf",
                    content_type='application/pdf',
                    size_bytes=file_size,
                    uploaded_by=session.get('username'),
                )
                db_session.add(doc)
                db_session.commit()
            finally:
                db_session.close()
        except Exception as doc_err:
            print(f"[EPIC IMPORT] Error saving document record: {doc_err}")

    # ── Step 5b: Fetch and process quote attachments (non-system-generated) ──
    quotes_imported = 0
    if policy_id and epic_client.is_configured:
        try:
            quote_attachments = epic_client.get_attachments_for_policy(
                policy_id, system_generated=False
            )
            for att in quote_attachments:
                file_info = att.get('file', {})
                file_url = file_info.get('url')
                ext = (file_info.get('extension') or '').lower()
                status = (file_info.get('status') or '').upper()

                if not file_url or ext not in ('.pdf', 'pdf') or status != 'OK':
                    continue

                try:
                    pdf_bytes = epic_client.download_attachment_file(file_url)
                    if not pdf_bytes or len(pdf_bytes) < 100:
                        continue

                    # Save quote PDF
                    upload_folder = current_app.config.get('UPLOAD_FOLDER', '/tmp')
                    quote_filename = f"epic_quote_{file_info.get('name', 'unknown')}_{datetime.now().strftime('%H%M%S')}.pdf"
                    quote_path = os.path.join(upload_folder, quote_filename)
                    with open(quote_path, 'wb') as f:
                        f.write(pdf_bytes)

                    # Parse as quote
                    from app.parsers.two_pass_parser import process_quote_two_pass
                    from app.database import create_quote
                    quote_result = process_quote_two_pass(quote_path)
                    extracted_json = json.dumps(quote_result.get('pass2_normalized', {}))
                    carrier_name = None
                    normalized = quote_result.get('pass2_normalized', {})
                    if 'policies' in normalized and normalized['policies']:
                        carrier_name = normalized['policies'][0].get('carrier')
                    if not carrier_name:
                        carrier_name = att.get('description', 'Unknown Carrier')

                    # Build a readable filename: "CarrierName_EffDate-ExpDate.pdf"
                    safe_carrier = (carrier_name or 'Unknown').replace(' ', '_').replace('/', '-')[:40]
                    readable_filename = f"{safe_carrier}_{effective_date}.pdf"
                    readable_path = os.path.join(upload_folder, readable_filename)
                    # Rename the temp file
                    if os.path.exists(quote_path) and not os.path.exists(readable_path):
                        os.rename(quote_path, readable_path)
                        quote_path = readable_path

                    quote_id = create_quote(
                        submission_id=submission_id,
                        carrier_name=carrier_name,
                        raw_document_path=quote_path,
                        extracted_json=extracted_json,
                        user=session.get('username'),
                        pass1_layout_json=json.dumps(quote_result.get('pass1_layout', {})) if quote_result.get('pass1_layout') else None,
                    )
                    quotes_imported += 1
                    print(f"[EPIC IMPORT] Quote imported: {carrier_name} (quote_id={quote_id})")

                except Exception as quote_err:
                    print(f"[EPIC IMPORT] Error processing quote attachment: {quote_err}")

        except EpicAPIError as e:
            print(f"[EPIC IMPORT] Could not fetch quote attachments: {e}")
        except Exception as e:
            print(f"[EPIC IMPORT] Unexpected error fetching quotes: {e}")

    # ── Step 5c: If quotes were imported, move to IN_PROGRESS (quoting stage) ──
    if quotes_imported > 0:
        db_session = get_session()
        try:
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if submission:
                from app.models import SubmissionStatus
                submission.status = SubmissionStatus.IN_PROGRESS
                db_session.commit()
        finally:
            db_session.close()

    # ── Step 6: Log ──
    log_action(
        entity_type='submission',
        entity_id=submission_id,
        action='imported_from_epic',
        user=session.get('username'),
        submission_id=submission_id,
        details=json.dumps({
            'epic_client_id': data.get('client_id'),
            'epic_policy_id': data.get('policy_id'),
            'epic_line_id': data.get('line_id'),
            'line_type': data.get('line_type'),
            'acord_125_downloaded': acord_pdf_path is not None,
            'acord_125_parsed': parsed_application is not None,
            'quotes_imported': quotes_imported,
        })
    )

    return jsonify({
        'success': True,
        'submission_id': submission_id,
        'message': f'Submission created for {client_name} from Epic',
        'acord_downloaded': acord_pdf_path is not None,
        'acord_parsed': parsed_application is not None,
    }), 201


@epic_bp.route('/api/epic/export/<int:submission_id>', methods=['POST'])
@login_required
def epic_export_submission(submission_id):
    """
    Export a bound submission back to Epic.
    Updates the line with carrier/premium/policy number and attaches all documents.
    """
    client = get_epic_client()
    if not client.is_configured:
        return jsonify({'success': False, 'error': 'Epic integration not configured'}), 400

    db_session = get_session()
    try:
        submission = db_session.query(Submission).filter_by(id=submission_id).first()
        if not submission:
            return jsonify({'success': False, 'error': 'Submission not found'}), 404

        if submission.ams_type != 'epic':
            return jsonify({'success': False, 'error': 'Submission is not linked to Epic'}), 400

        if not submission.epic_line_id or not submission.epic_policy_id:
            return jsonify({'success': False, 'error': 'Missing Epic line or policy ID'}), 400

        data = request.get_json() or {}

        # Build line update
        line_update = {}
        if data.get('issuing_company_code'):
            line_update['IssuingCompanyLookupCode'] = data['issuing_company_code']
        if data.get('estimated_premium') is not None:
            line_update['EstimatedPremium'] = data['estimated_premium']
        if data.get('status_code'):
            line_update['StatusCode'] = data['status_code']

        # Build policy update
        policy_update = {}
        if data.get('policy_number'):
            policy_update['PolicyNumber'] = data['policy_number']
        if data.get('description'):
            policy_update['Description'] = data['description']

        # Collect documents
        documents_to_attach = []
        docs = db_session.query(Document).filter_by(
            submission_id=submission_id, is_active=True
        ).all()

        for doc in docs:
            filepath = doc.storage_key if doc.storage_provider == 'local' and os.path.exists(doc.storage_key) else None
            documents_to_attach.append({
                'filepath': filepath,
                'filename': doc.original_filename,
                'content_type': doc.content_type or 'application/pdf',
                'description': f"{doc.document_type.value}: {doc.original_filename}",
            })

        # Execute export
        try:
            results = client.export_submission_to_epic(
                epic_policy_id=submission.epic_policy_id,
                epic_line_id=submission.epic_line_id,
                line_update_data=line_update if line_update else None,
                policy_update_data=policy_update if policy_update else None,
                documents=documents_to_attach if documents_to_attach else None,
            )
        except EpicAPIError as e:
            return jsonify({
                'success': False,
                'error': f'Epic API error during export: {e.detail}',
            }), 500

        # Mark as exported
        submission.epic_exported_at = datetime.utcnow()
        db_session.commit()

        log_action(
            entity_type='submission',
            entity_id=submission_id,
            action='exported_to_epic',
            user=session.get('username'),
            submission_id=submission_id,
            details=json.dumps(results)
        )

        return jsonify({
            'success': True,
            'message': 'Submission exported to Epic successfully',
            'results': results,
        })

    finally:
        db_session.close()
