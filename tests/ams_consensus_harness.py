"""
AMS Fill Consensus Harness
==========================
Runs each sample quote PDF through multiple Bedrock vision models against the
fake_epic.html field list. Outputs raw results to tests/ams_consensus_raw.json
so the consensus (majority-vote) answer key can be computed.

Usage:
    source myenv/bin/activate
    python tests/ams_consensus_harness.py

Takes ~5-10 minutes depending on model latency. Progress printed to stdout.
"""
import json
import os
import re
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.parsers.llm_parsers import BedrockClient
from PIL import Image
import pdfplumber

# ─── Configuration ────────────────────────────────────────────────────────────

MODELS = [
    # Three independent models — none are the production model (Sonnet 4.6)
    "us.anthropic.claude-opus-4-6-v1",
    "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
]

QUOTE_PDFS = [
    "sample_docs/quote_rooster.PDF",
    "sample_docs/quote_bull.pdf",
    "sample_docs/wolf/quote_wolf.pdf",
]

FAKE_EPIC_PATH = "app/static/fake_epic.html"
OUTPUT_PATH = "tests/ams_consensus_raw.json"
MAX_PAGES = 5
IMAGE_MAX_WIDTH = 1000
REGION = "us-east-1"


# ─── Field extraction from fake_epic.html ─────────────────────────────────────

def build_fields_from_fake_epic():
    """Parse fake_epic.html to get the field list as the extension would enumerate it."""
    html = open(FAKE_EPIC_PATH).read()
    fields = []

    for m in re.finditer(r'<(input|textarea|select)\b([^>]*?)>', html):
        tag, attrs = m.group(1), m.group(2)

        if tag == 'input':
            t = re.search(r'type="([^"]+)"', attrs)
            ftype = t.group(1) if t else 'text'
            if ftype in ('hidden', 'submit', 'button'):
                continue
        else:
            ftype = ''

        fid = re.search(r'id="([^"]+)"', attrs)
        if not fid:
            continue
        fid = fid.group(1)

        # Skip JS template literals (dynamic line items)
        if '${' in fid or '{' in fid:
            continue

        fname = re.search(r'name="([^"]+)"', attrs)

        # Find the nearest preceding label
        before = html[:m.start()]
        lab = re.findall(r'<label[^>]*>(.*?)</label>', before, re.S)
        label = re.sub(r'<[^>]+>', '', lab[-1]).strip() if lab else fid

        f = {
            's': f'#{fid}',
            'label': label,
        }

        if tag == 'select':
            f['type'] = 'select'
            block = html[m.end():html.index('</select>', m.end())]
            opts = []
            for opt_text in re.findall(r'<option[^>]*>([^<]*)</option>', block):
                opt_text = opt_text.strip()
                if opt_text and opt_text not in ('— Select —', '-- Select --'):
                    opts.append(opt_text)
            f['options'] = opts
        elif tag == 'textarea':
            f['type'] = 'textarea'
        elif ftype and ftype != 'text':
            f['type'] = ftype

        fields.append(f)

    return fields


# ─── PDF to page images ───────────────────────────────────────────────────────

def pdf_to_images(pdf_path, max_pages=MAX_PAGES):
    """Convert first N pages of a PDF to PIL images."""
    images = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= max_pages:
                break
            img = page.to_image(resolution=200).original
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            # Resize for consistency
            if img.width > IMAGE_MAX_WIDTH:
                ratio = IMAGE_MAX_WIDTH / img.width
                img = img.resize((IMAGE_MAX_WIDTH, int(img.height * ratio)), Image.LANCZOS)
            images.append(img)
    return images


# ─── Build prompt (same as the extension-fill endpoint) ───────────────────────

def build_prompt(fields_json, num_pages):
    """Build the same prompt used by /api/ams/extension-fill."""
    return (
        f"You are looking at {num_pages} images from an insurance quote document.\n\n"
        "Below is a list of empty form fields from an AMS (Agency Management System) web form. "
        "Each field has \"s\" (selector — use as JSON key), \"label\", optional \"type\", "
        "and for selects the allowed \"options\".\n\n"
        f"FORM FIELDS:\n{fields_json}\n\n"
        "YOUR TASK:\n"
        "1. Read the quote document images to extract ALL relevant insurance data.\n"
        "2. Match extracted data to the appropriate form fields.\n"
        "3. For dropdown/select fields, pick the closest matching option from that field's options list.\n"
        "4. Format: dates MM/DD/YYYY, currency digits only (no $), states as 2-letter codes.\n"
        "5. Type all text values in ALL CAPS, except select values which must match options exactly.\n"
        "6. Only match data you can clearly read from the quote — do not guess.\n"
        "7. Skip fields where no matching data exists in the quote.\n"
        "8. Broker field = wholesale broker. Producer field = retail agent.\n\n"
        "Return ONLY valid JSON mapping selector to {\"value\": \"...\"}\n"
        "Example: {\"#insured_name\":{\"value\":\"ACME CORP LLC\"},\"#state\":{\"value\":\"LA\"}}\n\n"
        "Include ALL fields you have confident matches for. Be thorough — check every field."
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    fields = build_fields_from_fake_epic()
    fields_json = json.dumps(fields, separators=(',', ':'))
    valid_selectors = {f['s'] for f in fields}

    print(f"Fields from fake_epic.html: {len(fields)}")
    print(f"Models to test: {len(MODELS)}")
    print(f"Quote PDFs: {len(QUOTE_PDFS)}")
    print(f"Total calls: {len(MODELS) * len(QUOTE_PDFS)}\n")

    # Load existing results if resuming
    results = {}
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH) as f:
            results = json.load(f)
        print(f"Loaded {sum(len(v) for v in results.values())} existing results\n")

    for pdf_path in QUOTE_PDFS:
        if not os.path.exists(pdf_path):
            print(f"  SKIP (not found): {pdf_path}")
            continue

        quote_key = os.path.basename(pdf_path).replace('.pdf', '').replace('.PDF', '')
        # Disambiguate quotes with same filename in different folders
        parent = os.path.basename(os.path.dirname(pdf_path))
        if parent and parent != 'sample_docs':
            # Check if another PDF in the list has the same basename
            same_name = [p for p in QUOTE_PDFS if os.path.basename(p).replace('.pdf','').replace('.PDF','') == quote_key and p != pdf_path]
            if same_name:
                quote_key = f"{parent}_{quote_key}"

        if quote_key not in results:
            results[quote_key] = {}

        # Convert PDF to images once per quote
        try:
            images = pdf_to_images(pdf_path)
        except Exception as e:
            print(f"  ERROR converting {pdf_path}: {e}")
            continue

        prompt = build_prompt(fields_json, len(images))
        print(f"── {quote_key} ({len(images)} pages) ──")

        for model in MODELS:
            model_short = model.split('.')[-1][:30]

            # Skip if already have this result
            if model in results[quote_key]:
                print(f"  {model_short:30s} → cached ({len(results[quote_key][model])} fields)")
                continue

            try:
                client = BedrockClient(model=model, region=REGION)
                t = time.time()
                fills = client.generate_json_with_images(prompt, images, max_width=IMAGE_MAX_WIDTH)
                elapsed = time.time() - t

                # Filter to valid selectors only
                clean = {}
                for selector, payload in (fills or {}).items():
                    if selector not in valid_selectors:
                        continue
                    if isinstance(payload, dict):
                        value = payload.get('value')
                    else:
                        value = payload
                    if value is not None and str(value).strip():
                        clean[selector] = str(value).strip()

                results[quote_key][model] = clean
                print(f"  {model_short:30s} → {len(clean):2d} fields in {elapsed:.1f}s")

            except Exception as e:
                print(f"  {model_short:30s} → ERROR: {e}")
                results[quote_key][model] = {"_error": str(e)}

            # Save after each call (resume-friendly)
            with open(OUTPUT_PATH, 'w') as f:
                json.dump(results, f, indent=2)

        print()

    print(f"\nDone. Raw results saved to {OUTPUT_PATH}")
    print(f"Quotes processed: {len(results)}")
    total_calls = sum(
        len(v) for v in results.values()
        if isinstance(v, dict)
    )
    print(f"Total model calls: {total_calls}")


if __name__ == '__main__':
    main()
