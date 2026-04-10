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
from app.parsers.llm_parsers import GeminiClient, GroqClient
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
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        print(f"  Processing {total_pages} page(s)...")
        for page_number, page in enumerate(pdf.pages, start=1):
            print(f"  Processing page {page_number}...")

            extracted_text = page.extract_text()
            use_ocr = (not extracted_text) or _is_text_garbage(extracted_text)

            if use_ocr:
                # Only use OCR when text extraction fails or is garbage
                if extracted_text:
                    print(f"    ⚠️  Extracted text is garbage/unreadable, using OCR...")
                else:
                    print(f"    ⚠️  No text extracted, using OCR...")

                text = _ocr_page_with_fallback(page)
                char_count = len(text)
                print(f"    ✓ OCR extracted {char_count} chars")
            else:
                # Text extraction is good - use it directly without OCR comparison
                text = extracted_text
                char_count = len(text)
                print(f"    ✓ Extracted {char_count} chars via text extraction")

            pages.append({"page_number": page_number, "text": text or ""})

            # Clean up memory after each page
            del extracted_text
            if 'text' in locals():
                del text
            gc.collect()

    return {"pages": pages}


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
    print("Pass 1: Extracting layout and OCR...")
    pass1_start = time.time()
    layout = pass1_extract_application_layout(pdf_path)
    metadata["pass1_duration"] = time.time() - pass1_start
    print(f"  ✓ Pass 1 complete ({metadata['pass1_duration']:.2f}s)")
    print(f"  Pass 1 data: {json.dumps(layout, indent=2)}")

    # Pass 2: Normalize to JSON
    print("Pass 2: Normalizing to JSON schema...")
    pass2_start = time.time()
    normalized = pass2_normalize_application_data(layout)
    metadata["pass2_duration"] = time.time() - pass2_start
    print(f"  ✓ Pass 2 complete ({metadata['pass2_duration']:.2f}s)")
    print(f"  Pass 2 data: {json.dumps(normalized, indent=2)}")

    metadata["total_duration"] = time.time() - start
    print(f"✓ All passes complete ({metadata['total_duration']:.2f}s)")

    return {
        "pass1_layout": layout,
        "pass2_normalized": normalized,
        "processing_metadata": metadata
    }

