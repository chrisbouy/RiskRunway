import re
from typing import List,Dict
from models.extracted_field import ExtractedField, Source

LABEL_PATTERN = re.compile(
    r"NAME\s*\(First Named Insured\)", re.IGNORECASE
)
START_PATTERN = re.compile(
    r"NAME\s*\(First\s+Named\s+Insured\).*MAILING\s+ADDRESS",
    re.IGNORECASE
)
END_PATTERNS = [
    re.compile(r"CORPORATION", re.IGNORECASE),
    re.compile(r"INDIVIDUAL", re.IGNORECASE),
    re.compile(r"LLC", re.IGNORECASE),
    re.compile(r"NAME\s*\(Other\s+Named\s+Insured\)", re.IGNORECASE),
]

def extract_applicant_info(lines: List[Dict]) -> Dict[str, Optional[str]]:
    """Extract applicant information from ACORD form"""
    
    result = {
        "company_name": None,
        "address": None,
        "city_state_zip": None,
        "email": None,
        "phone": None
    }
    
    print("\n=== FIRST 50 EXTRACTED LINES ===")
    for i, line in enumerate(lines[:50]):
        print(f"{i}: {line['text']}")
    
    # Look for key patterns anywhere in the document
    for i, line in enumerate(lines):
        text = line["text"]
        
        # Email pattern
        email_match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', text)
        if email_match:
            result["email"] = email_match.group()
            print(f"\n✓ Found email at line {i}: {email_match.group()}")
        
        # City, State ZIP pattern (CT 53495-5180)
        zip_match = re.search(r'([A-Za-z\s]+),?\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)', text)
        if zip_match:
            result["city_state_zip"] = zip_match.group().strip()
            print(f"✓ Found city/state/zip at line {i}: {zip_match.group()}")
        
        # Look for "NAME" label to find company name
        if re.search(r'NAME.*First.*Named.*Insured', text, re.IGNORECASE):
            print(f"\n✓ Found NAME label at line {i}")
            # Company name should be in next few lines
            for offset in range(1, 5):
                if i + offset < len(lines):
                    candidate = lines[i + offset]["text"].strip()
                    # Skip lines with common labels/keywords
                    if (candidate and 
                        len(candidate) > 3 and
                        not re.search(r'^(NAME|ADDRESS|MAILING|GL\s+CODE|SIC|WEBSITE|BUSINESS|PHONE)', candidate, re.IGNORECASE)):
                        result["company_name"] = candidate
                        print(f"✓ Found company name at line {i+offset}: {candidate}")
                        break
        
        # Street address pattern
        if re.search(r'\d{4,5}.*(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Circle|Village|Ste)', text, re.IGNORECASE):
            if not result["address"]:
                result["address"] = text.strip()
                print(f"✓ Found address at line {i}: {text.strip()}")
    
    return result
def extract_insured_name(lines: List[dict]) -> ExtractedField:
    for i, line in enumerate(lines):
        if LABEL_PATTERN.search(line["text"]):
            # insured name is usually on the same line or next line
            for lookahead in range(1, 4):
                if i + lookahead < len(lines):
                    candidate = lines[i + lookahead]["text"].strip()
                    if candidate:
                        return ExtractedField(
                            field_name="insured_name",
                            value=candidate,
                            confidence=0.9,
                            source=Source(
                                page=lines[i + lookahead]["page"],
                                text=candidate
                            ),
                            method="line_follow"
                        )

    return ExtractedField(
        field_name="insured_name",
        value=None,
        confidence=0.0,
        source=None,
        method="not_found"
    )
