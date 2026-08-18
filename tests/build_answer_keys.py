"""
Build Consensus Answer Keys
============================
Reads tests/ams_consensus_raw.json (produced by ams_consensus_harness.py) and
computes a majority-vote answer key per quote.

Rules:
- A field is included in the answer key if at least 2 of 3 models returned it.
- The value used is the most common value across models (case-insensitive match
  for text, exact match for numbers/dates).
- Fields where models disagree and no majority exists are flagged as "contested"
  and still included with the most common value, but marked in metadata.

Output: tests/ams_answer_keys.json
"""
import json
import os
import sys
from collections import Counter

RAW_PATH = "tests/ams_consensus_raw.json"
OUTPUT_PATH = "tests/ams_answer_keys.json"

# Minimum number of models that must agree for a field to be in the answer key.
MIN_AGREEMENT = 2


def normalize_value(v):
    """Normalize a value for comparison purposes."""
    if v is None:
        return None
    s = str(v).strip()
    # Normalize currency: strip trailing .00, leading zeros
    # But keep decimal precision for non-zero decimals
    if s.replace('.', '').replace(',', '').isdigit():
        try:
            num = float(s.replace(',', ''))
            # If it's a whole number, return without decimals
            if num == int(num) and '.' not in s.rstrip('0'):
                return str(int(num))
            # Otherwise keep as-is but strip trailing zeros after decimal
            return s.rstrip('0').rstrip('.') if '.' in s else s
        except ValueError:
            pass
    return s


def values_match(a, b):
    """Check if two values are equivalent (case-insensitive for text, normalized for numbers)."""
    na = normalize_value(a)
    nb = normalize_value(b)
    if na is None or nb is None:
        return False
    # Try case-insensitive comparison
    if na.upper() == nb.upper():
        return True
    # Try numeric comparison
    try:
        return abs(float(na.replace(',', '')) - float(nb.replace(',', ''))) < 0.01
    except (ValueError, TypeError):
        pass
    return False


def compute_consensus(model_results):
    """
    Given {model_id: {selector: value, ...}, ...}, compute the consensus answer key.
    Returns (answer_key, metadata) where:
      answer_key = {selector: value}
      metadata = {selector: {agreement, total_models, contested, all_values}}
    """
    # Only use successful results
    valid_results = {
        m: v for m, v in model_results.items()
        if isinstance(v, dict) and '_error' not in v
    }

    if not valid_results:
        return {}, {}

    num_models = len(valid_results)

    # Collect all selectors that any model returned
    all_selectors = set()
    for result in valid_results.values():
        all_selectors.update(result.keys())

    answer_key = {}
    metadata = {}

    for selector in sorted(all_selectors):
        # Collect values from each model for this selector
        values = []
        for model_id, result in valid_results.items():
            if selector in result:
                values.append(result[selector])

        if len(values) < MIN_AGREEMENT:
            continue

        # Group by normalized value to find the majority
        groups = {}
        for v in values:
            matched = False
            for key in groups:
                if values_match(v, key):
                    groups[key].append(v)
                    matched = True
                    break
            if not matched:
                groups[v] = [v]

        # Find the largest group
        best_group_key = max(groups, key=lambda k: len(groups[k]))
        agreement = len(groups[best_group_key])
        contested = agreement < len(values) and len(groups) > 1

        # Use the first value from the winning group (preserves original casing)
        winning_value = groups[best_group_key][0]

        answer_key[selector] = winning_value
        metadata[selector] = {
            'agreement': agreement,
            'total_models': num_models,
            'models_returned': len(values),
            'contested': contested,
            'all_values': values,
        }

    return answer_key, metadata


def main():
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    with open(RAW_PATH) as f:
        raw = json.load(f)

    answer_keys = {}
    summary = []

    for quote_key in sorted(raw.keys()):
        model_results = raw[quote_key]
        answer_key, metadata = compute_consensus(model_results)

        contested_fields = [s for s, m in metadata.items() if m['contested']]
        unanimous_fields = [s for s, m in metadata.items() if m['agreement'] == m['models_returned']]

        answer_keys[quote_key] = {
            'fields': answer_key,
            'metadata': {
                'total_fields': len(answer_key),
                'unanimous': len(unanimous_fields),
                'contested': len(contested_fields),
                'contested_selectors': contested_fields,
            }
        }

        summary.append(
            f"  {quote_key:30s} → {len(answer_key):2d} fields "
            f"({len(unanimous_fields)} unanimous, {len(contested_fields)} contested)"
        )

    # Write output
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(answer_keys, f, indent=2)

    print(f"Answer keys written to {OUTPUT_PATH}")
    print(f"Quotes: {len(answer_keys)}\n")
    for line in summary:
        print(line)

    # Print total stats
    total_fields = sum(v['metadata']['total_fields'] for v in answer_keys.values())
    total_unanimous = sum(v['metadata']['unanimous'] for v in answer_keys.values())
    total_contested = sum(v['metadata']['contested'] for v in answer_keys.values())
    print(f"\nTotals: {total_fields} fields, {total_unanimous} unanimous, {total_contested} contested")


if __name__ == '__main__':
    main()
