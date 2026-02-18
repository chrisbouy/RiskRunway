"""
Three-Pass AI Processing System for Insurance Quote Documents

Pass 1: OCR and Layout Extraction - Extract raw text preserving document structure
Pass 2: Normalization to JSON - Convert to standardized schema
Pass 3: Quote Intent Classification - Determine quote type and comparison strategy
"""

from textwrap import dedent
import json
import time

import time
import random
import requests
from google import genai
from google.genai import types
from app.parsers.llm_parsers import GeminiClient, GroqClient
import settings
import pdfplumber
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter
import io

DEFAULT_MODEL = "gemini-2.5-flash"

# ============================================================================
# PASS 1: OCR and Layout Extraction
# ============================================================================
PASS1_OCR_PROMPT = dedent(
    """
    You are performing OCR and layout extraction on an insurance quote document.
    
Extract ALL visible text from the document.

Rules:
- Preserve page breaks
- Preserve line order
- Do NOT infer section names
- Do NOT label content
- Do NOT interpret tables
- Represent tables as plain text rows exactly as seen
- Do NOT summarize or reorganize

Return:
{
  "pages": [
    {
      "page_number": 1,
      "text": "raw text exactly as seen"
    }
  ]
}
    """
)

# ============================================================================
# PASS 2: Normalization to JSON Schema
# ============================================================================
PASS2_NORMALIZATION_PROMPT = dedent(
    """
    You are normalizing extracted insurance quote data into a standardized JSON schema.
    
    INPUT: OCR text from an insurance quote document
    OUTPUT: Valid JSON only (no markdown, no explanations)

═══════════════════════════════════════════════════════════════
CRITICAL EXTRACTION RULES
═══════════════════════════════════════════════════════════════

1. ONLY extract values that are EXPLICITLY STATED in the document
   - If a value is ambiguous, unclear, or requires inference: return null
   - If multiple conflicting values exist: return null

2. NEVER extract a person's name into company/entity fields
   - ❌ BAD: "Carrier": "John Smith" 
   - ✓ GOOD: "Carrier": "Great American Insurance Company"
   - If only a person is listed, return null for that field

3. Each DISTINCT coverage type must be its own policy object
   - Do NOT combine or merge coverages
   - Even if they share the same carrier/dates

4. Policy-level fees/taxes ONLY if explicitly tied to that specific policy
   - If fees/taxes only appear in a totals section: leave policy fields null

5. Carrier = insurance company that assumes the risk
   - NOT a person, NOT a syndicate member name
   - "Underwritten by" ≠ automatically the carrier
   - If unclear which entity is the carrier: return null

═══════════════════════════════════════════════════════════════
FIELD EXTRACTION GUIDE (with synonyms)
═══════════════════════════════════════════════════════════════

INSURED (the customer buying insurance):
  • Label may appear as: "Insured", "Named Insured", "Applicant", "Borrower", 
    "Account Name", "Customer", "Firm Name", "DBA", "Policyholder"

GENERAL AGENT / WHOLESALE BROKER (wholesale intermediary, if present):
  • Company name may appear as: "General Agent", "MGA", "Wholesale Broker", 
    "Managing General Agent", "Broker", "Surplus Lines Broker"
  • This is a COMPANY, not a person
  • This is commonly the company that wrote the quote, NOT the carrier
  • The "agent name" subfield is for an individual contact person at that company

COVERAGE TYPE:
  • Normalize to standard terms:
    - "General Liability" (from: CGL, Commercial General Liability, GL)
    - "Workers Compensation" (from: WC, Work Comp, Workers Comp)
    - "Commercial Auto" (from: CA, Business Auto, Auto)
    - "Commercial Property" (from: CP, Property, Building)
    - "Professional Liability" (from: E&O, Errors & Omissions)
    - "Cyber Liability" (from: Cyber, Data Breach, Privacy)
    - "Directors & Officers" (from: D&O)
    - "Umbrella" (from: Excess, Umbrella Liability)
  • Use the standard term in your output, not the abbreviation

CARRIER (insurance company):
  • Label may appear as: "Carrier", "Underwriter", "Insurer", "Insurance Company", 
    "Underwriting Company", "Company", "Issuing Company"
  • Extract the COMPANY NAME, not person names
  • Common patterns to watch for:
    - "Underwritten by XYZ Insurance Company" → Carrier: "XYZ Insurance Company"
    - "Paper: ABC Mutual" → Carrier: "ABC Mutual"

POLICY NUMBER:
  • May appear as: "Policy No.", "Policy #", "Contract Number", "Reference Number"
  • Often labeled "TBD" or "To Be Determined" on quotes (extract as-is)

DATES:
  • Effective Date labels: "Eff Date", "Inception", "Policy Start", "Effective"
  • Expiration Date labels: "Exp Date", "Expiry", "Policy End", "Expiration"
  • Format all dates as: YYYY-MM-DD
  • If you see "12/31/2024", convert to "2024-12-31"

POLICY TERM:
  • May appear as: "Term", "Policy Period", "Coverage Period"
  • Extract as stated (e.g., "12 months", "1 year", "6 months")

PREMIUM:
  • May appear as: "Premium", "Annual Premium", "Total Premium", "Full Term Premium",
    "Written Premium", "Base Premium"
  • Extract the FULL TERM amount (not per-payment or per-month)

TAX:
  • May appear as: "Tax", "Surplus Lines Tax", "SL Tax", "State Tax", "Premium Tax"
  • May be shown as percentage or dollar amount (extract dollar amount)

FEE:
  • May appear as: "Fee", "Policy Fee", "Admin Fee", "Inspection Fee"
  • This is carrier fees, NOT broker fees

BROKER FEE:
  • May appear as: "Broker Fee", "Supplier Fee", "MGA Fee", "Wholesale Fee"
  • Separate from policy fees

MINIMUM EARNED:
  • May appear as: "Minimum Earned", "Min Earned", "Fully Earned", "Short Rate"
  • Can be percentage (e.g., "90%") or dollar amount
  • Extract percentage as decimal (90% → 90, not 0.90)

TOTALS SECTION:
  • Usually at bottom of document in a box, table, or summary
  • May be labeled: "Summary", "Payment Schedule", "Amount Due", "Total Due"
  • Extract:
    - Total Premium (sum of all premiums)
    - Total Tax (sum of all taxes)
    - Total Fee (sum of all fees, excluding broker fees)
    - Total Broker Fee (if shown separately)
    - Grand Total (final amount due)

DOWN PAYMENT / FINANCING:
  • May appear as: "Down Payment", "Deposit", "Required Down", "Initial Payment"
  • Amount Financed may be calculated as: Grand Total - Down Payment
  • Often NOT shown on quotes (return null if not present)

═══════════════════════════════════════════════════════════════
OUTPUT JSON SCHEMA
═══════════════════════════════════════════════════════════════

Return this EXACT structure (all fields required, use null if not found):

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
    "agency": {
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
    "general_agent_or_wholesale_broker": {
        "name": "string or null (company name)",
        "contact_person": "string or null (individual name)",
        "address": {
            "street": "string or null",
            "city": "string or null",
            "state": "string or null",
            "zip": "string or null"
        },
        "phone": "string or null",
        "fax": "string or null"
    },
    "quote_number": "string or null",
    "account_number": "string or null",
    "policies": [
        {
            "coverage_type": "string or null (use standard term, not abbreviation)",
            "carrier": "string or null (company name only)",
            "policy_number": "string or null",
            "effective_date": "string or null (YYYY-MM-DD format)",
            "expiration_date": "string or null (YYYY-MM-DD format)",
            "policy_term": "string or null",
            "annual_premium": "number or null",
            "tax": "number or null",
            "fee": "number or null",
            "broker_fee": "number or null",
            "minimum_earned_percent": "number or null (as whole number, e.g. 90 not 0.90)",
            "minimum_earned_amount": "number or null"
        }
    ],
    "totals": {
        "total_premium": "number or null",
        "total_tax": "number or null",
        "total_fee": "number or null",
        "total_broker_fee": "number or null",
        "grand_total": "number or null"
    },
    "financing": {
        "down_payment": "number or null",
        "amount_financed": "number or null"
    }
}

═══════════════════════════════════════════════════════════════
RETURN ONLY VALID JSON - NO MARKDOWN - NO EXPLANATIONS
═══════════════════════════════════════════════════════════════
"""
)

# ============================================================================
# PASS 3: Quote Intent Classification
# ============================================================================
PASS3_INTENT_PROMPT = dedent(
    """
    You are analyzing an insurance quote to determine its intent and relationship to other quotes.
    
    You will receive:
    1. The normalized quote data (JSON)
    2. Context about existing quotes in the submission (if any)
    
    Your job is to classify the quote's intent and identify how it should be displayed.
    
    QUOTE INTENT TYPES:
    - "new_coverage" - This quote adds NEW coverage types not previously quoted
    - "competing_quote" - This quote is for the SAME coverage(s) from a different carrier/broker (shopping quotes)
    - "renewal" - This quote renews existing coverage
    - "endorsement" - This quote modifies existing coverage
    - "unknown" - Cannot determine intent
    
    COMPARISON GROUPS:
    Common groups: "GL" (General Liability), "WC" (Workers Comp), "Auto", "Property", "Cyber", "E&O", "D&O", etc.
    
    INSTRUCTIONS:
    1. Identify which coverage types are in this quote
    2. Determine if these coverages already exist in the submission
    3. If they exist, this is likely a "competing_quote"
    4. If they don't exist, this is likely "new_coverage"
    5. Identify what makes this quote different from existing quotes (carrier, premium, terms, etc.)
    
    Return valid JSON only using this schema:
    {
        "quote_intent": "string - one of: new_coverage, competing_quote, renewal, endorsement, unknown",
        "applies_to_coverages": ["array of coverage type strings from the quote"],
        "comparison_groups": ["array of comparison group identifiers - e.g. GL, WC, Auto"],
        "competing_with_quote_ids": ["array of quote IDs this competes with, if applicable"],
        "key_differences": {
            "carrier": "boolean - different carrier?",
            "premium": "boolean - different premium?",
            "terms": "boolean - different terms/conditions?",
            "broker": "boolean - different broker?"
        },
        "notes": "string - brief explanation of the classification",
        "confidence": "string - high, medium, low"
    }
    
    Return ONLY valid JSON. No markdown. No explanations.
    """
)

def get_llm_client():
    if settings.LLM_PROVIDER == "groq":
        return GroqClient(settings.GROQ_API_KEY)
    if settings.LLM_PROVIDER == "gemini":
        return GeminiClient(genai.Client(api_key=settings.GEMINI_API_KEY), DEFAULT_MODEL)
    raise ValueError("Unknown LLM provider")
# ============================================================================
# Processing Functions
# ============================================================================
def pass1_extract_layout(pdf_path):
    """
    Pass 1: Extract text and layout from PDF using classic OCR (pdfplumber + pytesseract)

    Args:
        pdf_path: Path to the PDF file

    Returns:
        dict: Structured layout data with pages array
    """
    pages_data = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            print(f"  Processing page {page_num}...")

            # Try text extraction first (for digital PDFs)
            page_text = page.extract_text()

            if page_text and len(page_text.strip()) > 50:
                # Digital PDF with extractable text
                print(f"    ✓ Extracted {len(page_text)} chars via text extraction")
                pages_data.append({
                    "page_number": page_num,
                    "text": page_text
                })
            else:
                # Scanned PDF - use OCR
                print(f"    No text found, using OCR...actually skipping OCR for now")

                # Convert page to image at high resolution
                # page_image = page.to_image(resolution=300).original

                # # Try multiple preprocessing methods
                # best_text = ""
                # best_char_count = 0

                # for method_name, preprocess_func in [
                #     ("high_contrast", _preprocess_high_contrast),
                #     ("moderate", _preprocess_moderate),
                #     ("minimal", _preprocess_minimal)
                # ]:
                #     img = preprocess_func(page_image)

                #     # Try different Tesseract PSM modes
                #     for psm in [6, 11]:  # 6=uniform block, 11=sparse text
                #         try:
                #             config = f'--oem 3 --psm {psm}'
                #             text = pytesseract.image_to_string(img, config=config)

                #             if len(text) > best_char_count:
                #                 best_char_count = len(text)
                #                 best_text = text
                #         except Exception as e:
                #             print(f"      Error with {method_name} PSM {psm}: {e}")

                # print(f"    ✓ OCR extracted {best_char_count} chars")
                # pages_data.append({
                #     "page_number": page_num,
                #     "text": best_text
                # })

    return {
        "pages": pages_data
    }

def _preprocess_high_contrast(image):
    """High contrast preprocessing for OCR"""
    if not isinstance(image, Image.Image):
        image = Image.open(io.BytesIO(image))

    img = image.convert('L')

    # Increase contrast
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.5)

    # Increase sharpness
    enhancer = ImageEnhance.Sharpness(img)
    img = enhancer.enhance(2.0)

    # Threshold
    img = img.point(lambda x: 0 if x < 128 else 255, '1')

    return img

def _preprocess_moderate(image):
    """Moderate preprocessing for OCR"""
    if not isinstance(image, Image.Image):
        image = Image.open(io.BytesIO(image))

    img = image.convert('L')

    # Slight contrast boost
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.5)

    # Denoise
    img = img.filter(ImageFilter.MedianFilter(size=3))

    return img

def _preprocess_minimal(image):
    """Minimal preprocessing for OCR"""
    if not isinstance(image, Image.Image):
        image = Image.open(io.BytesIO(image))

    # Just convert to grayscale
    return image.convert('L')

def pass2_normalize_data(layout_data):
    """
    Pass 2: Normalize extracted layout into standard JSON schema
    
    Args:
        layout_data: Output from pass1_extract_layout
        
    Returns:
        dict: Normalized quote data
    """
    # client = genai.Client(api_key=settings.GEMINI_API_KEY)
    
    # Create prompt with layout data
    # prompt = PASS2_NORMALIZATION_PROMPT + "\n\nExtracted Layout Data:\n" + json.dumps(layout_data, indent=2)
    
    # response = client.models.generate_content(
    #     model=DEFAULT_MODEL,
    #     contents=prompt,
    #     config=types.GenerateContentConfig(
    #         temperature=0.1,
    #         response_mime_type="application/json"
    #     )
    # )
    
    # llm = GroqClient(api_key=settings.GROQ_API_KEY)
    llm = get_llm_client()

    prompt = PASS2_NORMALIZATION_PROMPT + "\n\nExtracted Layout Data:\n" + json.dumps(layout_data)
    # print(f"Prompt: {prompt}")

    normalized_data = groq_request_with_backoff(lambda: llm.generate_json(prompt))
    # normalized_data =  llm.generate_json(prompt)
    # print(f"Normalized data: {normalized_data}")
    # print(f"Normalized data: {json.dumps(normalized_data, indent=2)}")

    
    # Parse response
    # result_text = response.text.strip()
    result_text = json.dumps(normalized_data)
    # print(f"Result text: {result_text}")
    
    if result_text.startswith("```json"):
        result_text = result_text[7:]
    if result_text.endswith("```"):
        result_text = result_text[:-3]
    
    return json.loads(result_text.strip())

def pass3_classify_intent(normalized_data, existing_quotes=None):
    """
    Pass 3: Classify quote intent and determine comparison strategy

    Args:
        normalized_data: Output from pass2_normalize_data
        existing_quotes: List of existing quote data in the submission (optional)

    Returns:
        dict: Intent classification data
    """
    # client = genai.Client(api_key=settings.GEMINI_API_KEY)

    # # Build context about existing quotes
    context = {
        "new_quote": normalized_data,
        "existing_quotes": existing_quotes or []
    }

    # # Create prompt with context
    # prompt = PASS3_INTENT_PROMPT + "\n\nContext:\n" + json.dumps(context, indent=2)

    # response = client.models.generate_content(
    #     model=DEFAULT_MODEL,
    #     contents=prompt,
    #     config=types.GenerateContentConfig(
    #         temperature=0.1,
    #         response_mime_type="application/json"
    #     )
    # )
    llm = GroqClient(api_key=settings.GROQ_API_KEY)

    prompt = PASS3_INTENT_PROMPT + "\n\nContext:\n" + json.dumps(context, indent=2)

    normalized_data = llm.generate_json(prompt)

    # Parse response
    # result_text = response.text.strip()
    result_text = json.dumps(normalized_data)
    if result_text.startswith("```json"):
        result_text = result_text[7:]
    if result_text.endswith("```"):
        result_text = result_text[:-3]

    return json.loads(result_text.strip())

def process_quote_three_pass(pdf_path, existing_quotes=None):

    import time

    start_time = time.time()
    metadata = {}

    # Pass 1: Extract layout
    print("Pass 1: Extracting layout and OCR...")
    pass1_start = time.time()
    layout_data = pass1_extract_layout(pdf_path)
    metadata['pass1_duration'] = time.time() - pass1_start
    print(f"  ✓ Pass 1 complete ({metadata['pass1_duration']:.2f}s)")
    # print(f"  Pass 1 data: {json.dumps(layout_data, indent=2)}")

    # Pass 2: Normalize to JSON
    print("Pass 2: Normalizing to JSON schema...")
    pass2_start = time.time()
    normalized_data = pass2_normalize_data(json.dumps(layout_data))
    metadata['pass2_duration'] = time.time() - pass2_start
    print(f"  ✓ Pass 2 complete ({metadata['pass2_duration']:.2f}s)")
    # print(f"  Pass 2 data: {json.dumps(normalized_data, indent=2)}")

    # Pass 3: Classify intent
    # print("Pass 3: Classifying quote intent...")
    # pass3_start = time.time()
    # intent_data = pass3_classify_intent(normalized_data, existing_quotes)
    # metadata['pass3_duration'] = time.time() - pass3_start
    # print(f"  ✓ Pass 3 complete ({metadata['pass3_duration']:.2f}s)")
    # print(f"  Pass 3 data: {json.dumps(intent_data, indent=2)}")

    metadata['total_duration'] = time.time() - start_time
    print(f"✓ All passes complete ({metadata['total_duration']:.2f}s)")

    return {
        "pass1_layout": layout_data,
        "pass2_normalized": normalized_data,
        # "pass3_intent": intent_data,
        "processing_metadata": metadata
    }

# Backward compatibility function
def parse_quote(pdf_path):
    """
    Backward compatible function that returns just the normalized data
    (for existing code that expects the old single-pass behavior)
    """
    result = process_quote_three_pass(pdf_path)
    # print(f"parse_quote result: {result}")
    return result["pass2_normalized"]

def groq_request_with_backoff(fn, max_retries=5):
    for attempt in range(max_retries):
        try:
            return fn()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                wait = (2 ** attempt) + random.uniform(0, 1)
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Groq rate limit exceeded after retries")


