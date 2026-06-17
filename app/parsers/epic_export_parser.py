"""
Epic Export Parser

Extracts data from bound quote/binder PDFs specifically for populating
the Applied Epic SDK payload. This parser focuses on fields NOT already
captured in the quoting stage parse — primarily coverage limits and
commission information.

The export flow pre-fills from previous parses (app + quote), then uses
this parser to extract the remaining SDK-specific fields.
"""

import json
import os
from textwrap import dedent

from app.parsers.llm_parsers import BedrockClient, GeminiClient, GroqClient
from app.parsers.two_pass_parser import pass1_extract_quote_layout, get_llm_client


EPIC_EXPORT_PROMPT = dedent("""
    You are extracting structured data from a commercial insurance quote or binder PDF
    to populate an Applied Epic SDK API payload for policy export.

    From the document, extract values for the following fields and return ONLY valid JSON.
    If a value cannot be found, use null.

    CRITICAL RULES:
    1. Only extract values EXPLICITLY stated in the document
    2. Carrier = the insurance company assuming risk (not a person, not the MGA)
    3. All dollar amounts as numbers (no $ sign, no commas)
    4. All dates in ISO 8601 format (YYYY-MM-DD)
    5. All percentages as whole numbers (15% = 15, not 0.15)
    6. Coverage limits as numbers (1000000 not "1,000,000" or "$1M")

    FIELD GUIDE:

    POLICY NUMBER: May appear as "Policy No.", "Policy #", "Binder No.", "Quote No."
    May be "TBD" on quotes — extract as-is.

    CARRIER / ISSUING COMPANY: The insurance company name. 
    "Underwritten by", "Insurer", "Carrier", "Paper"

    LINE TYPE: Normalize to standard Epic codes:
    - "GL" for General Liability / CGL
    - "PKG" for Commercial Package
    - "WC" for Workers Compensation
    - "CA" for Commercial Auto
    - "CP" for Commercial Property
    - "PL" for Professional Liability / E&O
    - "CY" for Cyber Liability
    - "DO" for Directors & Officers
    - "UMB" for Umbrella / Excess

    PREMIUM: The base annual premium BEFORE taxes and fees.

    COMMISSION: Agency commission percentage or dollar amount if stated.
    May appear as "Commission", "Agency Commission", "Producer Commission".

    OUTPUT JSON SCHEMA:
    {
        "policy_number": "string or null",
        "carrier_name": "string or null (full company name)",
        "line_type_code": "string or null (GL, PKG, WC, CA, CP, PL, CY, DO, UMB)",
        "line_type_description": "string or null (full name like 'General Liability')",
        "effective_date": "string or null (YYYY-MM-DD)",
        "expiration_date": "string or null (YYYY-MM-DD)",
        "estimated_premium": "number or null (annual base premium)",
        "annualized_premium": "number or null (same as estimated if annual term)",
        "billed_premium": "number or null (total billed including fees if shown)",
        "agency_commission_percent": "number or null (as whole number)",
        "agency_commission_amount": "number or null",
        "status_code": "string or null (BOUND, NEW, QUOTED — based on document type)"
    }

    RETURN ONLY VALID JSON — NO MARKDOWN — NO EXPLANATIONS
""")


def parse_for_epic_export(pdf_path):
    """
    Parse a quote/binder PDF specifically for Epic SDK export fields.
    
    Uses the same Pass 1 (layout extraction) as the quote parser,
    then a different Pass 2 prompt focused on SDK fields + limits.
    
    Returns:
        dict with the export-specific parsed fields
    """
    from PIL import Image

    # Pass 1: Extract layout (we only need the page images for vision)
    print("[EPIC EXPORT PARSER] Pass 1: Extracting page images...")
    layout_data = pass1_extract_quote_layout(pdf_path, max_pages=None)

    # Pass 2: Always use vision path (images) regardless of digital/scanned
    # Tabular data like limits reads much better from images than extracted text
    print("[EPIC EXPORT PARSER] Pass 2: Extracting SDK fields + limits (vision)...")
    llm = get_llm_client()

    images = []
    vision_max_width = 1000
    for page in layout_data.get("pages", []):
        img_path = page.get("image_path")
        if img_path and os.path.exists(img_path):
            img = Image.open(img_path).convert("RGB")
            if img.width > vision_max_width:
                ratio = vision_max_width / img.width
                new_size = (vision_max_width, int(img.height * ratio))
                img = img.resize(new_size, Image.LANCZOS)
            images.append(img)

    if not images:
        raise ValueError("No page images available for export parsing")

    num_pages = len(images)
    prompt = (
        f"You are looking at {num_pages} page(s) from an insurance document.\n\n"
        "Read ALL pages carefully — coverage limits and deductibles are often on later pages in tables.\n\n"
        + EPIC_EXPORT_PROMPT
    )

    print(f"  Sending {num_pages} page image(s) to vision model...")
    normalized_data = llm.generate_json_with_images(prompt, images)

    if isinstance(normalized_data, str):
        result_text = normalized_data
        if result_text.startswith("```json"):
            result_text = result_text[7:]
        if result_text.endswith("```"):
            result_text = result_text[:-3]
        normalized_data = json.loads(result_text.strip())

    print("[EPIC EXPORT PARSER] ✓ Export parse complete")
    return normalized_data


def build_epic_export_payload(submission_data, winning_quote_data, export_parsed_data):
    """
    Combine data from all three parse stages into the final SDK payload.
    
    Priority (highest wins):
    1. export_parsed_data (freshest, has limits)
    2. winning_quote_data (from quoting stage extracted_json)
    3. submission_data (from app/submission_intake)
    
    Args:
        submission_data: dict from submission.submission_intake (app parse)
        winning_quote_data: dict from quote.extracted_json (quote parse, pass2_normalized)
        export_parsed_data: dict from parse_for_epic_export (this module)
    
    Returns:
        dict shaped for the confirmation modal, containing line_update and policy_update
    """
    # Extract from quote parse
    quote_policy = {}
    if winning_quote_data and winning_quote_data.get('policies'):
        quote_policy = winning_quote_data['policies'][0] if winning_quote_data['policies'] else {}

    # Build the combined payload with fallback chain
    effective_date = (
        export_parsed_data.get('effective_date')
        or quote_policy.get('effective_date')
        or (submission_data or {}).get('effective_date')
    )
    expiration_date = (
        export_parsed_data.get('expiration_date')
        or quote_policy.get('expiration_date')
    )
    carrier = (
        export_parsed_data.get('carrier_name')
        or quote_policy.get('carrier')
    )
    premium = (
        export_parsed_data.get('estimated_premium')
        or quote_policy.get('annual_premium')
    )
    policy_number = (
        export_parsed_data.get('policy_number')
        or quote_policy.get('policy_number')
    )

    return {
        # For display in confirmation modal
        'carrier_name': carrier,
        'policy_number': policy_number,
        'effective_date': effective_date,
        'expiration_date': expiration_date,
        'estimated_premium': premium,
        'annualized_premium': export_parsed_data.get('annualized_premium') or premium,
        'billed_premium': export_parsed_data.get('billed_premium'),
        'line_type_code': export_parsed_data.get('line_type_code'),
        'line_type_description': export_parsed_data.get('line_type_description') or quote_policy.get('coverage_type'),
        'agency_commission_percent': export_parsed_data.get('agency_commission_percent'),
        'agency_commission_amount': export_parsed_data.get('agency_commission_amount'),
        'status_code': export_parsed_data.get('status_code') or 'BOUND',
        
        # SDK-shaped payloads ready for the API call
        'sdk_line_update': {
            'IssuingCompanyLookupCode': carrier,
            'EstimatedPremium': premium,
            'AnnualizedPremium': export_parsed_data.get('annualized_premium') or premium,
            'BilledPremium': export_parsed_data.get('billed_premium') or premium,
            'AgencyCommissionPercent': export_parsed_data.get('agency_commission_percent'),
            'AgencyCommissionAmount': export_parsed_data.get('agency_commission_amount'),
            'StatusCode': export_parsed_data.get('status_code') or 'BOUND',
            'LineTypeCode': export_parsed_data.get('line_type_code'),
            'LineTypeDescription': export_parsed_data.get('line_type_description') or quote_policy.get('coverage_type'),
        },
        'sdk_policy_update': {
            'PolicyNumber': policy_number,
            'Description': export_parsed_data.get('line_type_description') or quote_policy.get('coverage_type'),
            'EffectiveDate': effective_date,
            'ExpirationDate': expiration_date,
            'EstimatedPremium': premium,
            'AnnualizedPremium': export_parsed_data.get('annualized_premium') or premium,
        },
    }
