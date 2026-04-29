"""
Two-pass parsing pipeline for insurance applications (e.g., ACORD 125).

This parser is intentionally separate from quote parsing:
- Pass 1 focuses on robust text extraction for intake forms.
- Pass 2 normalizes only submission-intake fields (not quote pricing data).
"""

from textwrap import dedent
import json
import time

import pdfplumber
import pytesseract
from google import genai

import settings
from app.parsers.llm_parsers import BedrockClient, GeminiClient, GroqClient
from app.parsers.two_pass_parser import groq_request_with_backoff, _is_text_garbage

DEFAULT_MODEL = "gemini-2.5-flash"

PASS2_APPLICATION_PROMPT = dedent(
    """
    You are extracting CLIENT + SUBMISSION intake data from an insurance APPLICATION document.
    This is NOT a quote comparison task.

    INPUT:
    - OCR/layout text extracted from an application (often ACORD 125).

    OUTPUT:
    - Return ONLY valid JSON.
    - No markdown. No explanations.

    CRITICAL RULES:
    1) Extract only explicitly stated values.
    2) If uncertain, return null.
    3) "Insured name" should come from fields like:
       - "NAME (First Named Insured)"
       - "Applicant"
       - "Named Insured"
    4) Do not confuse city/state/ZIP with insured name.
    5) Do not extract policy premium/tax/fee totals here.
    6) Do not include wholesale broker/MGA in output, even if present.
    7) Coverage types needed should be an array of normalized strings
       (e.g., General Liability, Commercial Property, Workers Compensation, Commercial Auto).

    Return this exact schema:
    {
      "insured": {
        "name": "string or null",
        "address": {
          "street": "string or null",
          "city": "string or null",
          "state": "string or null",
          "zip": "string or null"
        }
      },
      "retail_agent": {
        "name": "string or null",
        "code": "string or null",
        "address": {
          "street": "string or null",
          "city": "string or null",
          "state": "string or null",
          "zip": "string or null"
        },
        "phone": "string or null"
      },
      "quote_number": "string or null",
      "account_number": "string or null",
      "submission": {
        "effective_date": "YYYY-MM-DD string or null",
        "expiration_date": "YYYY-MM-DD string or null",
        "policy_or_program_name": "string or null",
        "coverage_types_needed": ["array of strings"]
      }
    }
    """
)


def _get_llm_client():
    if settings.LLM_PROVIDER == "groq":
        return GroqClient(settings.GROQ_API_KEY)
    if settings.LLM_PROVIDER == "bedrock":
        return BedrockClient()
    if settings.LLM_PROVIDER == "gemini":
        return GeminiClient(genai.Client(api_key=settings.GEMINI_API_KEY), DEFAULT_MODEL)
    raise ValueError("Unknown LLM provider")


def _ocr_page_with_fallback(page):
    page_image = page.to_image(resolution=200).original
    try:
        # Use single best config for insurance docs (PSM 6 = uniform block of text)
        config = "--oem 3 --psm 6"
        text = pytesseract.image_to_string(page_image, config=config)
        return text
    except Exception:
        return ""


def pass1_extract_application_layout(pdf_path):
    import gc
    pages_data = []

    # First, find the last relevant page to avoid processing useless pages
    last_page_to_process = _find_last_relevant_page(pdf_path)

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            # Skip pages after financial data
            if page_num > last_page_to_process:
                print(f"  Skipping page {page_num} (after financial data)")
                continue

            print(f"  Processing page {page_num}...")

            # Try text extraction first (for digital PDFs)
            page_text = page.extract_text()

            # Check if extracted text is usable or garbage
            if page_text and not _is_text_garbage(page_text):
                # Digital PDF with good extractable text
                print(f"    ✓ Extracted {len(page_text)} chars via text extraction")
                pages_data.append({
                    "page_number": page_num,
                    "text": page_text
                })
                # Clean up memory after each page
                del page_text
                gc.collect()
            else:
                # Either no text, or garbage text - use OCR
                if page_text:
                    print(f"    ⚠️  Extracted text is garbage/unreadable, using OCR...")
                else:
                    print(f"    ⚠️  Scanned PDF, using OCR...")

                # Convert page to image at 300 DPI (good balance of quality/speed)
                page_image = page.to_image(resolution=300).original

                # Single-pass OCR with best settings for insurance docs
                # PSM 6 = uniform block of text (best for structured documents)
                # OEM 3 = default (LSTM + legacy)
                try:
                    config = '--oem 3 --psm 6'
                    text = pytesseract.image_to_string(page_image, config=config)
                    char_count = len(text)
                    print(f"    ✓ OCR extracted {char_count} chars")

                    pages_data.append({
                        "page_number": page_num,
                        "text": text
                    })

                    # Clean up memory
                    del page_image, text
                    gc.collect()
                except Exception as e:
                    print(f"    ✗ OCR failed: {e}")
                    # Add empty page so we don't skip it entirely
                    pages_data.append({
                        "page_number": page_num,
                        "text": ""
                    })
                    # Clean up on error too
                    if 'page_image' in locals():
                        del page_image
                    gc.collect()

    return {
        "pages": pages_data
    }

def _find_last_relevant_page(pdf_path):
    """
    Quick scan to find the last page with financial data.
    Looks for actual financial summary patterns, not just generic keywords.

    Returns:
        int: Last page number to process (1-indexed), or total pages if not found
    """
    import re

    last_relevant_page = 0

    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        print(f"  Quick scan: checking {total_pages} pages for financial data...")

        for page_num, page in enumerate(pdf.pages, start=1):
            # Try text extraction first (fast for digital PDFs)
            page_text = page.extract_text()

            if not page_text or len(page_text.strip()) < 50:
                # Scanned page - do quick low-res OCR
                try:
                    page_image = page.to_image(resolution=150).original  # Low res for speed
                    config = '--oem 3 --psm 6'
                    page_text = pytesseract.image_to_string(page_image, config=config)
                except:
                    page_text = ""

            # Look for actual financial data patterns (dollar amounts with context)
            # These patterns indicate real financial summaries, not just headers
            page_text_lower = page_text.lower()

            # Pattern 1: Dollar amounts near financial terms (e.g., "Total: $3,255.20")
            has_financial_amount = bool(re.search(r'(total|premium|tax|fee|deposit|financed|due)[\s:$]*\$?\d+[,\d]*\.?\d*', page_text_lower))

            # Pattern 2: Multiple dollar amounts (indicates a financial table/summary)
            dollar_amounts = re.findall(r'\$\s*\d+[,\d]*\.?\d{2}', page_text)
            has_multiple_amounts = len(dollar_amounts) >= 3

            # Pattern 3: Specific financial summary phrases
            summary_phrases = [
                'grand total', 'total payable', 'amount financed',
                'down payment', 'payment schedule', 'total due',
                'premium breakdown', 'total premium', 'total tax', 'total fee'
            ]
            has_summary_phrase = any(phrase in page_text_lower for phrase in summary_phrases)

            # Page is relevant if it has financial amounts AND context
            if (has_financial_amount and has_multiple_amounts) or has_summary_phrase:
                last_relevant_page = page_num
                print(f"    Page {page_num}: Found financial data")

        if last_relevant_page == 0:
            # No financial data found, process first 3 pages only (safety fallback)
            # Most quotes have financial data on first page
            fallback_pages = min(3, total_pages)
            print(f"  ⚠️  No financial data detected, processing first {fallback_pages} pages as fallback")
            return fallback_pages
        else:
            # No buffer needed - we're detecting actual financial content, not just keywords
            print(f"  ✓ Last financial data on page {last_relevant_page}, will process {last_relevant_page}/{total_pages} pages")
            return last_relevant_page

def _postprocess_application_data(data):
    if not isinstance(data, dict):
        return {
            "insured": {"name": None, "address": {"street": None, "city": None, "state": None, "zip": None}},
            "retail_agent": {"name": None, "code": None, "address": {"street": None, "city": None, "state": None, "zip": None}, "phone": None},
            "quote_number": None,
            "account_number": None,
            "submission": {"effective_date": None, "expiration_date": None, "policy_or_program_name": None, "coverage_types_needed": []}
        }

    submission = data.get("submission") or {}
    coverages = submission.get("coverage_types_needed") or []
    if not isinstance(coverages, list):
        coverages = []

    # Normalize to unique non-empty strings.
    normalized_coverages = []
    for item in coverages:
        if not item:
            continue
        value = str(item).strip()
        if value and value not in normalized_coverages:
            normalized_coverages.append(value)
    submission["coverage_types_needed"] = normalized_coverages
    data["submission"] = submission
    return data


def pass2_normalize_application_data(layout_data):
    llm = _get_llm_client()
    prompt = PASS2_APPLICATION_PROMPT + "\n\nExtracted Layout Data:\n" + json.dumps(layout_data)

    print(f"  Sending to LLM for normalization...")
    normalized = groq_request_with_backoff(lambda: llm.generate_json(prompt))
    print(f"  ✓ LLM normalization complete")

    return _postprocess_application_data(normalized)


def process_application_two_pass(pdf_path):
    start = time.time()
    metadata = {}

    # Pass 1: Extract layout and OCR
    print("Pass 1 of application_parser.process_application_two_pass: Extracting layout and OCR...")
    pass1_start = time.time()
    layout = pass1_extract_application_layout(pdf_path)
    metadata["pass1_duration"] = time.time() - pass1_start
    print(f"  ✓ Pass 1 (application) complete ({metadata['pass1_duration']:.2f}s)")
    print(f"  Pass 1 data: {json.dumps(layout, indent=2)}")

    # Pass 2: Normalize to JSON
    print("Pass 2 of application_parser.process_application_two_pass: Normalizing to JSON schema...")
    pass2_start = time.time()
    normalized = pass2_normalize_application_data(layout)
    metadata["pass2_duration"] = time.time() - pass2_start
    print(f"  ✓ Pass 2 (application) complete ({metadata['pass2_duration']:.2f}s)")
    print(f"  Pass 2 data: {json.dumps(normalized, indent=2)}")

    metadata["total_duration"] = time.time() - start
    print(f"✓ All application passes complete ({metadata['total_duration']:.2f}s)")

    return {
        "pass1_layout": layout,
        "pass2_normalized": normalized,
        "processing_metadata": metadata
    }

