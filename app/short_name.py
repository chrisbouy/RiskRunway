# app/short_name.py
"""
Generate abbreviated insured names for mobile display using LLM.
Falls back to simple truncation if LLM is unavailable.
"""
import logging
import boto3
import json
from settings import BEDROCK_MODEL, BEDROCK_REGION

logger = logging.getLogger(__name__)

_bedrock_client = None


def _get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    return _bedrock_client


def generate_short_name(insured_name: str) -> str:
    """
    Generate a short, recognizable version of an insured name for mobile display.
    Target: 1-2 words, max ~15 chars. Must be immediately recognizable as the same entity.
    
    Examples:
        "Tree Frogs Adventure Parks LLC" → "Tree Frogs"
        "Acme Manufacturing Inc." → "Acme"
        "Johnson & Johnson Family Holdings" → "J&J"
        "Dr. Robert Smith DDS" → "Dr. Smith"
        "Louisiana Crawfish Company" → "LA Crawfish"
    """
    if not insured_name or not insured_name.strip():
        return ""

    name = insured_name.strip()

    # If it's already short enough, just use it
    if len(name) <= 12:
        return name

    try:
        client = _get_bedrock_client()
        prompt = (
            f'Shorten this business/insured name to a 1-2 word nickname (max 15 characters) '
            f'that an insurance agent would immediately recognize. Rules:\n'
            f'- Drop suffixes: LLC, Inc, Corp, Co, Ltd, DBA, etc.\n'
            f'- Use common abbreviations when the full name is well-known '
            f'(e.g. "Johnson & Johnson" → "J&J", "International Business Machines" → "IBM")\n'
            f'- For multi-word names, keep the most distinctive/memorable word(s) '
            f'(e.g. "Tree Frogs Adventure Parks" → "Tree Frogs", "Acme Manufacturing" → "Acme")\n'
            f'- For personal names, use last name (e.g. "Dr. Robert Smith DDS" → "Dr. Smith")\n'
            f'- Return ONLY the short name, nothing else. No quotes, no explanation.\n\n'
            f'Name: {name}'
        )

        response = client.converse(
            modelId=BEDROCK_MODEL,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 30, "temperature": 0}
        )

        content_blocks = response["output"]["message"]["content"]
        short = "".join(block.get("text", "") for block in content_blocks).strip()

        # Strip quotes if the model wrapped it
        short = short.strip('"\'')

        # Sanity check: if result is empty or longer than original, fall back
        if not short or len(short) > len(name):
            return _fallback_short_name(name)

        return short

    except Exception as e:
        logger.warning(f"Short name generation failed for '{name}': {e}")
        return _fallback_short_name(name)


def _fallback_short_name(name: str) -> str:
    """Simple heuristic fallback: take first word(s) up to ~12 chars, dropping suffixes."""
    suffixes = {'llc', 'inc', 'inc.', 'corp', 'corp.', 'co', 'co.', 'ltd', 'ltd.', 'l.l.c.', 'l.l.c'}
    words = name.split()
    # Remove trailing suffix words
    while words and words[-1].lower().rstrip('.,') in suffixes:
        words.pop()
    if not words:
        return name[:12]

    # Take words until we hit ~12 chars, but don't break on & or short connectors
    result = words[0]
    for w in words[1:]:
        candidate = result + ' ' + w
        if len(candidate) > 14 and w != '&':
            break
        if len(candidate) > 16:
            break
        result = candidate

    return result
