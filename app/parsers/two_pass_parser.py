"""
Three-Pass AI Processing System for Insurance Quote Documents

Pass 1: OCR and Layout Extraction - Extract raw text preserving document structure
Pass 2: Normalization to JSON - Convert to standardized schema
Pass 3: Quote Intent Classification - Determine quote type and comparison strategy
"""

from textwrap import dedent
import json
import os
import time

import time
import random
import requests
from google import genai
from google.genai import types
from app.parsers.llm_parsers import BedrockClient, GeminiClient, GroqClient
import settings
import pdfplumber
import pytesseract

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
    You are normalizing insurance quote data into a standardized JSON schema.
    
    INPUT: an insurance quote document
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
        
    RETAIL AGENT ...
    • This is a COMPANY, not a person
    • Usually located in the SAME STATE as the insured
    • May have a "Producer Code" or "Agent Code"
    • Only fill this field when a separate retail/producing agent is explicitly identified.
    • IMPORTANT: On wholesale broker proposals/cover letters, the retail agent is often 
        the "To:" recipient at the top of the document — the company the proposal is 
        addressed to. If a company name and address appear in a "To:" block, treat that 
        as the retail agent.
    • "Attn:" following a company name indicates a contact person, not the company name itself.
    
    GENERAL AGENT / WHOLESALE BROKER ...
    • This is the producer/proposer of the quote. Company that originally created the document.
    • If only one agency appears on the quote and there is no separately labeled retail/producer agency, assume this agency is the quote producer/wholesale broker.
    • Do not use the same agency for `retail_agent` unless the document explicitly names it as the customer's retail agent.
    • Address may appear as "Company Name - City, State" format on a single line
        (e.g. "Amwins - Baton Rouge, LA"). In this case extract city and state only,
        street should be null.
    • Do NOT extract city name as street address.
    • If no street number is present, street must be null.
    
    ADDRESSES:
    • If a zip code appears joined to a state abbreviation (e.g. LA70002), treat as state=LA zip=70002
    • Suite/Floor/Unit on the line immediately following a street address should be 
        appended to the street field with a comma (e.g. "3850 N. Causeway Blvd., Suite 1150")
    • Never insert spaces into quote numbers, policy numbers, or reference numbers
        even if they appear to have missing spaces
        
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
    • The BASE premium BEFORE taxes and fees
    • "Base Premium", "Written Premium", "Annual Premium" = correct field
    • "Total Annual Premium", "Total Due", "Amount Due" = WRONG - 
        this includes taxes/fees, do NOT use this as premium
    • Extract the FULL TERM amount (not per-payment or per-month)


    TAX:
    • May appear as: "Tax", "Surplus Lines Tax", "SL Tax", "State Tax", "Premium Tax"
    • May be shown as percentage or dollar amount (extract dollar amount)

    FEE:
    • May appear as: "Fee", "Policy Fee", "Admin Fee", "Inspection Fee"
    • If multiple carrier fees exist (Policy Fee, Inspection Fee, Admin Fee),
        sum them into a single total fee value
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
        - Total Premium (sum of all premiums) -The BASE premium BEFORE taxes and fees
        - Total Tax (sum of all taxes)
        - Total Fee (sum of all fees, excluding broker fees)
        - Total Broker Fee (if shown separately)
        - Grand Total (final amount due) - The final amount due, including taxes and fees

    DOWN PAYMENT / FINANCING:
    • May appear as: "Down Payment", "Deposit", "Required Down", "Initial Payment"
    • Amount Financed may be calculated as: Grand Total - Down Payment
    • Often NOT shown on quotes (return null if not present)
    • "Minimum and Deposit" or "Annual Minimum and Deposit" is NOT a down payment — 
        it refers to the minimum earned premium requirement, not a financing arrangement.
    • Only populate down_payment when an explicit installment/financing plan is shown
        with language like "Down Payment", "Initial Payment", "Amount Due at Inception"
        alongside remaining installment amounts.
    • If no financing schedule is present, both down_payment and amount_financed should be null.

    NOTES:
    - If a phone number follows an address, it's likely a contact number for that entity. 
    - All numbers (except number of months in policy term) should have two decimal places (e.g., 254.50, not 254.5)
    ═══════════════════════════════════════════════════════════════
    OUTPUT JSON SCHEMA
    ═══════════════════════════════════════════════════════════════

    Return this EXACT structure (all fields required, use null if not found):

    {
        "insured": {
            "name": "string or null",
            "address": {
                "street": "string or null - include suite/floor/unit if present in the document. If no suite is present, do NOT add any placeholder text. Return only what is explicitly in the document.",
                "city": "string or null",
                "state": "string or null",
                "zip": "string or null"
            }
        },
        "retail_agent": {
            "name": "string or null (company name)",
            "code": "string or null",
            "address": {
                "street": "string or null - include suite/floor/unit if present in the document. If no suite is present, do NOT add any placeholder text. Return only what is explicitly in the document.",
                "city": "string or null",
                "state": "string or null",
                "zip": "string or null"
            },
            "phone": "string or null"
        },
        "general_agent_or_wholesale_broker": {
            "name": "string or null (company name)",
            "address": {
                "street": "string or null - include suite/floor/unit if present in the document. If no suite is present, do NOT add any placeholder text. Return only what is explicitly in the document.",
                "city": "string or null",
                "state": "string or null",
                "zip": "string or null"
            },
            "phone": "string or null",
            "fax": "string or null"
        },
        "quote_number": "string or null",
        "policies": [
            {
                "coverage_type": "string or null (use standard term, not abbreviation)",
                "carrier": "string or null (company name only)",
                "policy_number": "string or null",
                "effective_date": "string or null (YYYY-MM-DD format)",
                "expiration_date": "string or null (YYYY-MM-DD format)",
                "policy_term": "number or null (number of months)",
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



def get_llm_client():
    provider = settings.LLM_PROVIDER.lower()
    if provider == "groq":
        if not settings.GROQ_API_KEY:
            raise ValueError("GROQ_API_KEY is required when LLM_PROVIDER=groq")
        return GroqClient(settings.GROQ_API_KEY, model=settings.GROQ_MODEL)
    if provider == "bedrock":
        return BedrockClient(model=settings.BEDROCK_MODEL, region=settings.BEDROCK_REGION)
    raise ValueError(f"Unknown LLM provider: {settings.LLM_PROVIDER}")
    # return BedrockClient(model=settings.BEDROCK_MODEL, region=settings.BEDROCK_REGION)
# ============================================================================
# Processing Functions
# ============================================================================
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

def _is_text_garbage(text):
    """
    Check if extracted text is garbage/unreadable.

    Returns True if text appears to be garbled, encoded incorrectly, or unusable.
    """
    if not text or len(text.strip()) < 50:
        return True

    # Check for CID encoding artifacts (common in PDFs with font issues)
    # Pattern: (cid:0), (cid:1), etc.
    if '(cid:' in text:
        cid_count = text.count('(cid:')
        # If more than 5% of text is CID references, it's garbage
        if cid_count > len(text) * 0.05 / 7:  # Each CID is ~7 chars
            return True

    # Count readable ASCII characters vs total characters
    readable_chars = sum(1 for c in text if c.isalnum() or c.isspace() or c in '.,;:!?-$()[]{}/@#%&*+=')
    total_chars = len(text)

    if total_chars == 0:
        return True

    readable_ratio = readable_chars / total_chars

    # If less than 70% of characters are readable, it's probably garbage
    if readable_ratio < 0.7:
        return True

    # Check for excessive special characters or encoding artifacts
    # Common garbage patterns: lots of �, ?, boxes, etc.
    garbage_chars = text.count('�') + text.count('\ufffd')
    if garbage_chars > total_chars * 0.1:  # More than 10% garbage chars
        return True

    # Check if text has reasonable word-like patterns
    # Split by whitespace and count "words" (sequences with letters)
    words = text.split()
    word_like = sum(1 for w in words if any(c.isalpha() for c in w))

    if len(words) > 10 and word_like / len(words) < 0.5:
        # Less than 50% of tokens look like words
        return True

    return False

def _fix_pdf_spacing(text):
    import re
    # Fix missing space between word and capitalized word (CausewayBlvd → Causeway Blvd)
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    # Fix missing space between US state abbreviation and zip code (LA70002 → LA 70002)
    text = re.sub(r'\b([A-Z]{2})(\d{5})\b', r'\1 \2', text)
    return text


def pass1_extract_quote_layout(pdf_path, max_pages=5):
    """
    Pass 1: Extract quote data from PDF.
    - Digital PDFs: extract tables + header text via pdfplumber (fast, no vision needed)
    - Scanned PDFs: save page images for vision model in Pass 2
    
    Args:
        pdf_path: Path to the PDF file
        max_pages: Maximum pages to process (None = all pages, default 5)
    """
    import gc

    pages_data = []
    is_scanned = False

    # Determine where to save page images (same directory as the PDF)
    pdf_dir = os.path.dirname(os.path.abspath(pdf_path))
    pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]

    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        last_page_to_process = total_pages if max_pages is None else min(max_pages, total_pages)
        pages_to_process = last_page_to_process
        print(f"  Processing {pages_to_process}/{total_pages} pages...")

        # Check first page to determine if scanned or digital
        first_page_text = pdf.pages[0].extract_text() if pdf.pages else None
        is_scanned = not first_page_text or _is_text_garbage(first_page_text)

        if is_scanned:
            print(f"  Detected: SCANNED PDF → using vision model path")
        else:
            print(f"  Detected: DIGITAL PDF → using tables + header path")

        for page_num, page in enumerate(pdf.pages, start=1):
            if page_num > last_page_to_process:
                break

            # Always save page image (needed for AMS vision export + scanned PDF path)
            page_image = page.to_image(resolution=200).original
            page_image_filename = f"{pdf_stem}_page_{page_num}.jpg"
            page_image_path = os.path.join(pdf_dir, page_image_filename)

            if page_image.mode in ("RGBA", "P", "LA"):
                page_image = page_image.convert("RGB")
            page_image.save(page_image_path, format="JPEG", quality=80)
            print(f"    ✓ Page {page_num} → {page_image_path}")

            if is_scanned:
                # Scanned: just save image path, vision model will read it in Pass 2
                pages_data.append({
                    "page_number": page_num,
                    "image_path": page_image_path
                })
            else:
                # Digital: extract full page text (fast, no OCR needed)
                page_text = page.extract_text() or ""
                full_text = _fix_pdf_spacing(page_text)

                print(f"      ✓ {len(full_text)} chars extracted")
                pages_data.append({
                    "page_number": page_num,
                    "text": full_text,
                    "image_path": page_image_path
                })

            del page_image
            gc.collect()

    return {
        "pages": pages_data,
        "total_pages": total_pages,
        "pages_processed": len(pages_data),
        "is_scanned": is_scanned
    }


def pass2_normalize_quote_data(layout_data):
    """
    Pass 2: Normalize to structured JSON.
    - Scanned PDFs: send page images to vision model (it reads the doc directly)
    - Digital PDFs: send extracted text to text-based LLM
    """
    from PIL import Image

    llm = get_llm_client()
    is_scanned = layout_data.get("is_scanned", False)

    if is_scanned:
        # Vision path: send images to model
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
            raise ValueError("No page images available for vision extraction")

        num_pages = len(images)
        prompt = (
            f"You are looking at {num_pages} page(s) from an insurance quote document.\n\n"
            "Read the document images and extract all relevant data into the JSON schema below.\n\n"
            + PASS2_NORMALIZATION_PROMPT
        )

        print(f"  Sending {num_pages} page image(s) to vision model...")
        normalized_data = llm.generate_json_with_images(prompt, images)
    else:
        # Text path: send extracted tables + headers to LLM
        lean_data = {
            "pages": [
                {"page_number": p["page_number"], "text": p.get("text", "")}
                for p in layout_data.get("pages", [])
            ]
        }

        prompt = PASS2_NORMALIZATION_PROMPT + "\n\nExtracted Layout Data:\n" + json.dumps(lean_data)
        print(f"  Sending extracted text to LLM...")
        normalized_data = llm.generate_json(prompt)

    if isinstance(normalized_data, dict):
        return normalized_data

    result_text = json.dumps(normalized_data)
    if result_text.startswith("```json"):
        result_text = result_text[7:]
    if result_text.endswith("```"):
        result_text = result_text[:-3]

    return json.loads(result_text.strip())

def process_quote_two_pass(pdf_path, existing_quotes=None):

    import time

    start_time = time.time()
    metadata = {}

    # Pass 1: Extract layout (detects scanned vs digital)
    print("Pass 1: Extracting quote layout...")
    pass1_start = time.time()
    layout_data = pass1_extract_quote_layout(pdf_path)
    metadata['pass1_duration'] = time.time() - pass1_start
    print(f"  ✓ Pass 1 complete ({metadata['pass1_duration']:.2f}s)")

    # Pass 2: Normalize (vision for scanned, text LLM for digital)
    print("Pass 2: Normalizing to JSON schema...")
    pass2_start = time.time()
    normalized_data = pass2_normalize_quote_data(layout_data)
    metadata['pass2_duration'] = time.time() - pass2_start
    print(f"  ✓ Pass 2 complete ({metadata['pass2_duration']:.2f}s)")

    metadata['total_duration'] = time.time() - start_time
    print(f"✓ All quote passes complete ({metadata['total_duration']:.2f}s)")

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
    result = process_quote_two_pass(pdf_path)
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

