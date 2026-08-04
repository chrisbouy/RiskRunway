"""
Canonical coverage type normalization.

Ensures that coverage types extracted from applications, quotes, and user input
all resolve to the same canonical string regardless of how the source document
words them (e.g., "GL", "General Liability", "Commercial General Liability"
all become "General Liability").

Used by:
- application_parser.py (post-processing parsed coverage_types_needed)
- two_pass_parser.py (post-processing parsed policies[].coverage_type)
- kanban.html (client-side filter dropdown — keep CANONICAL_COVERAGE_TYPES in sync)
"""

# Canonical coverage type names (the "one true" string stored in the database)
CANONICAL_COVERAGE_TYPES = [
    "General Liability",
    "Commercial Property",
    "Workers Compensation",
    "Commercial Auto",
    "Business Owners Policy",
    "Umbrella/Excess",
    "Professional Liability",
    "Directors & Officers",
    "Employment Practices",
    "Cyber Liability",
    "Inland Marine",
    "Crime/Fidelity",
    "Liquor Liability",
    "Products Liability",
    "Pollution Liability",
    "Builder's Risk",
    "Garage Liability",
    "Ocean Marine",
    "Aviation",
    "Surety Bond",
    "Flood",
    "Earthquake",
]

# Map of lowercase alias → canonical name
_ALIASES = {
    # General Liability
    "gl": "General Liability",
    "general liability": "General Liability",
    "commercial general liability": "General Liability",
    "cgl": "General Liability",
    "comm general liability": "General Liability",
    "commercial gl": "General Liability",

    # Commercial Property
    "property": "Commercial Property",
    "commercial property": "Commercial Property",
    "cp": "Commercial Property",
    "comm property": "Commercial Property",

    # Workers Compensation
    "wc": "Workers Compensation",
    "workers comp": "Workers Compensation",
    "workers compensation": "Workers Compensation",
    "worker's compensation": "Workers Compensation",
    "worker's comp": "Workers Compensation",
    "workerscomp": "Workers Compensation",
    "work comp": "Workers Compensation",

    # Commercial Auto
    "auto": "Commercial Auto",
    "commercial auto": "Commercial Auto",
    "business auto": "Commercial Auto",
    "ca": "Commercial Auto",
    "comm auto": "Commercial Auto",
    "commercial automobile": "Commercial Auto",
    "business automobile": "Commercial Auto",

    # Business Owners Policy
    "bop": "Business Owners Policy",
    "business owners policy": "Business Owners Policy",
    "business owners": "Business Owners Policy",
    "businessowners": "Business Owners Policy",
    "business owner's policy": "Business Owners Policy",

    # Umbrella / Excess
    "umbrella": "Umbrella/Excess",
    "excess": "Umbrella/Excess",
    "excess liability": "Umbrella/Excess",
    "umbrella/excess": "Umbrella/Excess",
    "umbrella / excess": "Umbrella/Excess",
    "umbrella liability": "Umbrella/Excess",
    "commercial umbrella": "Umbrella/Excess",
    "excess/umbrella": "Umbrella/Excess",

    # Professional Liability
    "professional liability": "Professional Liability",
    "e&o": "Professional Liability",
    "errors and omissions": "Professional Liability",
    "errors & omissions": "Professional Liability",
    "pl": "Professional Liability",
    "professional indemnity": "Professional Liability",

    # Directors & Officers
    "d&o": "Directors & Officers",
    "directors and officers": "Directors & Officers",
    "directors & officers": "Directors & Officers",
    "d & o": "Directors & Officers",
    "directors and officers liability": "Directors & Officers",

    # Employment Practices
    "epli": "Employment Practices",
    "employment practices liability": "Employment Practices",
    "employment practices": "Employment Practices",
    "employment practices liability insurance": "Employment Practices",

    # Cyber
    "cyber": "Cyber Liability",
    "cyber liability": "Cyber Liability",
    "cyber insurance": "Cyber Liability",
    "cyber risk": "Cyber Liability",

    # Inland Marine
    "inland marine": "Inland Marine",
    "im": "Inland Marine",

    # Crime / Fidelity
    "crime": "Crime/Fidelity",
    "fidelity": "Crime/Fidelity",
    "crime/fidelity": "Crime/Fidelity",
    "crime / fidelity": "Crime/Fidelity",
    "fidelity bond": "Crime/Fidelity",
    "employee dishonesty": "Crime/Fidelity",

    # Liquor Liability
    "liquor liability": "Liquor Liability",
    "liquor": "Liquor Liability",

    # Products Liability
    "products liability": "Products Liability",
    "products": "Products Liability",
    "products/completed operations": "Products Liability",

    # Pollution
    "pollution liability": "Pollution Liability",
    "pollution": "Pollution Liability",
    "environmental liability": "Pollution Liability",
    "environmental": "Pollution Liability",

    # Builder's Risk
    "builder's risk": "Builder's Risk",
    "builders risk": "Builder's Risk",
    "builder risk": "Builder's Risk",

    # Garage
    "garage liability": "Garage Liability",
    "garage": "Garage Liability",
    "garagekeepers": "Garage Liability",

    # Ocean Marine
    "ocean marine": "Ocean Marine",
    "marine": "Ocean Marine",

    # Aviation
    "aviation": "Aviation",
    "aircraft": "Aviation",

    # Surety
    "surety bond": "Surety Bond",
    "surety": "Surety Bond",
    "bond": "Surety Bond",

    # Flood
    "flood": "Flood",
    "flood insurance": "Flood",

    # Earthquake
    "earthquake": "Earthquake",
    "quake": "Earthquake",
}

# Also add canonical names (lowercase) pointing to themselves for easy lookup
for _name in CANONICAL_COVERAGE_TYPES:
    _lower = _name.lower()
    if _lower not in _ALIASES:
        _ALIASES[_lower] = _name


def normalize_coverage_type(raw: str) -> str:
    """
    Normalize a coverage type string to its canonical form.

    If the value matches a known alias, returns the canonical name.
    Otherwise returns the original string with leading/trailing whitespace stripped.

    Examples:
        normalize_coverage_type("GL") -> "General Liability"
        normalize_coverage_type("Commercial General Liability") -> "General Liability"
        normalize_coverage_type("workers comp") -> "Workers Compensation"
        normalize_coverage_type("Some Niche Coverage") -> "Some Niche Coverage"
    """
    if not raw:
        return ""
    cleaned = raw.strip()
    lookup = cleaned.lower()
    return _ALIASES.get(lookup, cleaned)


def normalize_coverage_list(coverages: list) -> list:
    """
    Normalize a list of coverage type strings, deduplicating after normalization.

    Returns a list of unique canonical coverage type strings preserving first-seen order.
    """
    if not coverages:
        return []
    seen = set()
    result = []
    for item in coverages:
        if not item:
            continue
        normalized = normalize_coverage_type(str(item))
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result
