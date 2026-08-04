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

import settings
from app.parsers.llm_parsers import BedrockClient, GeminiClient, GroqClient
from app.parsers.two_pass_parser import groq_request_with_backoff, _is_text_garbage

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
    7) Coverage types needed should be an array of normalized strings.
       Use ONLY these canonical names when possible:
       General Liability, Commercial Property, Workers Compensation, Commercial Auto,
       Business Owners Policy, Umbrella/Excess, Professional Liability, Directors & Officers,
       Employment Practices, Cyber Liability, Inland Marine, Crime/Fidelity,
       Liquor Liability, Products Liability, Pollution Liability, Builder's Risk,
       Garage Liability, Ocean Marine, Aviation, Surety Bond, Flood, Earthquake.
       If the coverage does not match any of these, use the standard industry term.

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
    provider = settings.LLM_PROVIDER.lower()
    if provider == "groq":
        if not settings.GROQ_API_KEY:
            raise ValueError("GROQ_API_KEY is required when LLM_PROVIDER=groq")
        return GroqClient(settings.GROQ_API_KEY, model=settings.GROQ_MODEL)
    if provider == "bedrock":
        return BedrockClient(model=settings.BEDROCK_MODEL, region=settings.BEDROCK_REGION)
    raise ValueError(f"Unknown LLM provider: {settings.LLM_PROVIDER}")


def pass1_extract_application_layout(pdf_path, max_pages=3):
    """
    Extract text from application PDF pages.
    For digital PDFs: uses pdfplumber text extraction (fast, accurate).
    For scanned PDFs: returns page images for direct vision LLM processing.
    
    Returns:
        dict with 'pages' list and 'has_scanned_pages' flag.
        Each page has 'page_number' and either 'text' or 'image' (PIL Image).
    """
    pages_data = []
    has_scanned_pages = False
    import gc

    with pdfplumber.open(pdf_path) as pdf:
        pages_to_process = min(max_pages, len(pdf.pages))
        print(f"  Processing first {pages_to_process} pages...")

        for page_num, page in enumerate(pdf.pages, start=1):
            if page_num > pages_to_process:
                break
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
                del page_text
                gc.collect()
            else:
                # Scanned page — capture image for vision LLM (skip Tesseract)
                has_scanned_pages = True
                if page_text:
                    print(f"    ⚠️  Extracted text is garbage/unreadable, capturing image for vision AI...")
                else:
                    print(f"    ⚠️  Scanned PDF, capturing image for vision AI...")

                # Render at low DPI and cap dimensions to keep payload under Groq's limit
                # Groq has a ~20MB payload limit; PNG-encoded pages must stay small
                MAX_DIMENSION = 2048  # Max width or height in pixels
                page_image = page.to_image(resolution=150).original

                # Downscale to fit within max dimension while preserving aspect ratio
                w, h = page_image.size
                if w > MAX_DIMENSION or h > MAX_DIMENSION:
                    scale = min(MAX_DIMENSION / w, MAX_DIMENSION / h)
                    new_w = int(w * scale)
                    new_h = int(h * scale)
                    page_image = page_image.resize((new_w, new_h))
                    print(f"    ↓ Resized from {w}x{h} to {new_w}x{new_h}")

                pages_data.append({
                    "page_number": page_num,
                    "image": page_image
                })
                gc.collect()

    return {
        "pages": pages_data,
        "has_scanned_pages": has_scanned_pages
    }

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

    # Normalize to unique non-empty strings using canonical coverage names.
    from app.parsers.coverage_normalizer import normalize_coverage_list
    normalized_coverages = normalize_coverage_list(coverages)
    submission["coverage_types_needed"] = normalized_coverages
    data["submission"] = submission
    return data


def pass2_normalize_application_data(layout_data):
    """
    Pass 2: Normalize extracted application data.
    
    If scanned pages are present (images), sends them directly to the vision LLM.
    If all pages are digital text, uses the text-only LLM path.
    """
    llm = _get_llm_client()
    has_scanned = layout_data.get("has_scanned_pages", False)

    if has_scanned:
        # Vision path: send page images directly to the LLM
        images = []
        text_pages = []
        for page in layout_data["pages"]:
            if "image" in page:
                images.append(page["image"])
            else:
                text_pages.append(page)

        # Build prompt — include any digital text pages as context
        prompt = PASS2_APPLICATION_PROMPT
        if text_pages:
            prompt += "\n\nAdditional text extracted from digital pages:\n" + json.dumps(text_pages)
        prompt += "\n\nThe attached images are scanned pages from the application. Extract the data directly from what you see in the images."

        print(f"  Sending {len(images)} page image(s) to vision LLM for extraction...")
        normalized = groq_request_with_backoff(lambda: llm.generate_json_with_images(prompt, images))
        print(f"  ✓ Vision LLM extraction complete")
    else:
        # Text-only path: all pages had good extractable text
        text_data = [{"page_number": p["page_number"], "text": p["text"]} for p in layout_data["pages"]]
        prompt = PASS2_APPLICATION_PROMPT + "\n\nExtracted Layout Data:\n" + json.dumps(text_data)

        print(f"  Sending to LLM for normalization...")
        normalized = groq_request_with_backoff(lambda: llm.generate_json(prompt))
        print(f"  ✓ LLM normalization complete")

    return _postprocess_application_data(normalized)


def process_application_two_pass(pdf_path):
    start = time.time()
    metadata = {}

    # Pass 1: Extract layout (text for digital pages, images for scanned pages)
    print("Pass 1 of application_parser.process_application_two_pass: Extracting layout...")
    pass1_start = time.time()
    layout = pass1_extract_application_layout(pdf_path, max_pages=3)
    metadata["pass1_duration"] = time.time() - pass1_start
    has_scanned = layout.get("has_scanned_pages", False)
    print(f"  ✓ Pass 1 (application) complete ({metadata['pass1_duration']:.2f}s) — {'vision path' if has_scanned else 'text path'}")

    # Log text pages only (images can't be serialized)
    text_pages = [p for p in layout["pages"] if "text" in p]
    # if text_pages:
        # print(f"  Pass 1 text data: {json.dumps(text_pages, indent=2)}")
    if has_scanned:
        image_pages = [p["page_number"] for p in layout["pages"] if "image" in p]
        # print(f"  Pass 1 scanned pages (sent as images): {image_pages}")

    # Pass 2: Normalize to JSON (uses vision for scanned pages)
    print("Pass 2 of application_parser.process_application_two_pass: Normalizing to JSON schema...")
    pass2_start = time.time()
    normalized = pass2_normalize_application_data(layout)
    metadata["pass2_duration"] = time.time() - pass2_start
    print(f"  ✓ Pass 2 (application) complete ({metadata['pass2_duration']:.2f}s)")
    # print(f"  Pass 2 data: {json.dumps(normalized, indent=2)}")

    metadata["total_duration"] = time.time() - start
    metadata["extraction_method"] = "vision" if has_scanned else "text"
    print(f"✓ All application passes complete ({metadata['total_duration']:.2f}s)")

    return {
        "pass1_layout": {"pages": text_pages} if text_pages else {"pages": []},
        "pass2_normalized": normalized,
        "processing_metadata": metadata
    }
