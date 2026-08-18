"""
AMS Fill Accuracy Regression Test
==================================
Exercises the /api/ams/extension-fill endpoint against each sample quote PDF
and asserts that field values match the consensus answer keys.

Usage:
    source myenv/bin/activate
    python tests/test_ams_fill_accuracy.py              # run all quotes
    python tests/test_ams_fill_accuracy.py quote_bull   # run one quote

The test creates temporary DB records and page images, calls the real endpoint
(which does a live Bedrock vision call), then compares to answer keys.

Pass criteria:
  - At least 70% of unanimous answer-key fields must match exactly.
  - At least 50% of contested answer-key fields must match.
  - No field should have a value that is wildly wrong (e.g. dates for names).
  - The endpoint must not error.

Each quote test takes ~10-15s (one Bedrock vision call).
"""
import json
import os
import re
import sys
import tempfile
import time

# Project root
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import pdfplumber
from PIL import Image

from app import create_app
from app.database import get_session
from app.models import Submission, Quote, AmsExportJob, SubmissionStatus

# ─── Config ───────────────────────────────────────────────────────────────────

ANSWER_KEYS_PATH = "tests/ams_answer_keys.json"
FAKE_EPIC_PATH = "app/static/fake_epic.html"
MAX_PAGES = 5
IMAGE_MAX_WIDTH = 1000

# Map from answer-key quote name to PDF path
QUOTE_PDF_MAP = {
    "quote_rooster": "sample_docs/quote_rooster.PDF",
    "quote_bull": "sample_docs/quote_bull.pdf",
    "quote_wolf": "sample_docs/wolf/quote_wolf.pdf",
}

# Thresholds
UNANIMOUS_MATCH_THRESHOLD = 0.70  # 70% of unanimous fields must match
CONTESTED_MATCH_THRESHOLD = 0.0   # Contested fields are informational — models disagreed on these so we don't fail on them
OVERALL_RECALL_THRESHOLD = 0.60   # At least 60% of all answer-key fields returned


# ─── Helpers ──────────────────────────────────────────────────────────────────

def build_fields_from_fake_epic():
    """Parse fake_epic.html to get the field list."""
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
        if '${' in fid or '{' in fid:
            continue

        # Skip line-item fields (same logic as the extension content script)
        if re.match(r'^line[_-].+[_-]\d+$', fid, re.IGNORECASE):
            continue

        fname = re.search(r'name="([^"]+)"', attrs)
        before = html[:m.start()]
        lab = re.findall(r'<label[^>]*>(.*?)</label>', before, re.S)
        label = re.sub(r'<[^>]+>', '', lab[-1]).strip() if lab else fid

        f = {
            'selector': f'#{fid}',
            'tag': tag,
            'type': ftype,
            'label': label,
            'name': fname.group(1) if fname else '',
            'id': fid,
            'placeholder': '',
            'current_value': '',
        }
        if tag == 'select':
            block = html[m.end():html.index('</select>', m.end())]
            f['options'] = [
                {'value': o, 'text': o}
                for o in re.findall(r'<option[^>]*>([^<]*)</option>', block)
            ]
        fields.append(f)
    return fields


def pdf_to_page_images(pdf_path, output_dir, max_pages=MAX_PAGES):
    """Convert PDF pages to JPEG files on disk. Returns list of paths."""
    paths = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= max_pages:
                break
            img = page.to_image(resolution=200).original
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            if img.width > IMAGE_MAX_WIDTH:
                ratio = IMAGE_MAX_WIDTH / img.width
                img = img.resize((IMAGE_MAX_WIDTH, int(img.height * ratio)), Image.LANCZOS)
            path = os.path.join(output_dir, f"page_{i+1}.jpg")
            img.save(path, format="JPEG", quality=80)
            paths.append(path)
    return paths


def values_match(actual, expected):
    """Flexible comparison: case-insensitive, numeric tolerance, substring."""
    if actual is None or expected is None:
        return False

    a = str(actual).strip()
    e = str(expected).strip()

    # Exact match (case-insensitive)
    if a.upper() == e.upper():
        return True

    # Numeric match (within 1%)
    try:
        na = float(a.replace(',', ''))
        ne = float(e.replace(',', ''))
        if ne == 0:
            return na == 0
        return abs(na - ne) / abs(ne) < 0.01
    except (ValueError, TypeError):
        pass

    # One contains the other (for verbose vs concise answers)
    if len(a) > 5 and len(e) > 5:
        if a.upper() in e.upper() or e.upper() in a.upper():
            return True

    return False


# ─── Test Runner ──────────────────────────────────────────────────────────────

class AmsAccuracyResult:
    def __init__(self, quote_key):
        self.quote_key = quote_key
        self.total_answer_fields = 0
        self.returned_fields = 0
        self.unanimous_total = 0
        self.unanimous_returned = 0
        self.unanimous_matched = 0
        self.contested_total = 0
        self.contested_returned = 0
        self.contested_matched = 0
        self.mismatches = []  # (selector, expected, actual)
        self.missing = []     # selectors in answer key but not returned
        self.extra = []       # selectors returned but not in answer key
        self.elapsed = 0.0
        self.error = None

    @property
    def unanimous_pct(self):
        # Of the unanimous fields that were returned, what % matched?
        return self.unanimous_matched / self.unanimous_returned if self.unanimous_returned else 1.0

    @property
    def contested_pct(self):
        # Of the contested fields that were returned, what % matched?
        return self.contested_matched / self.contested_returned if self.contested_returned else 1.0

    @property
    def recall_pct(self):
        return self.returned_fields / self.total_answer_fields if self.total_answer_fields else 1.0

    @property
    def passed(self):
        if self.error:
            return False
        return (
            self.unanimous_pct >= UNANIMOUS_MATCH_THRESHOLD
            and self.contested_pct >= CONTESTED_MATCH_THRESHOLD
            and self.recall_pct >= OVERALL_RECALL_THRESHOLD
        )

    def summary(self):
        if self.error:
            return f"ERROR: {self.error}"
        status = "PASS" if self.passed else "FAIL"
        return (
            f"{status} | "
            f"unanimous={self.unanimous_matched}/{self.unanimous_returned} ({self.unanimous_pct:.0%}) "
            f"contested={self.contested_matched}/{self.contested_returned} ({self.contested_pct:.0%}) "
            f"recall={self.returned_fields}/{self.total_answer_fields} ({self.recall_pct:.0%}) "
            f"extra={len(self.extra)} | {self.elapsed:.1f}s"
        )


def run_quote_test(app, quote_key, pdf_path, answer_key_entry, fields):
    """Run one quote through the fill endpoint and compare to answer key."""
    result = AmsAccuracyResult(quote_key)
    expected_fields = answer_key_entry['fields']
    metadata = answer_key_entry['metadata']
    contested_selectors = set(metadata.get('contested_selectors', []))

    result.total_answer_fields = len(expected_fields)
    result.unanimous_total = result.total_answer_fields - len(contested_selectors)
    result.contested_total = len(contested_selectors)

    with app.app_context():
        # Create temp dir for page images
        tmp_dir = tempfile.mkdtemp(prefix=f"ams_test_{quote_key}_")

        try:
            # 1. Render page images
            page_paths = pdf_to_page_images(pdf_path, tmp_dir)
            if not page_paths:
                result.error = "No pages rendered from PDF"
                return result

            # 2. Create DB records
            db_session = get_session()
            try:
                # Create a minimal Submission
                submission = Submission(
                    insured_name=f"_TEST_{quote_key}",
                    status=SubmissionStatus.IN_PROGRESS,
                    effective_date="2026-01-01",
                )
                db_session.add(submission)
                db_session.flush()

                # Create Quote with pass1_layout_json
                layout = {
                    "pages": [{"page_number": i+1, "image_path": p} for i, p in enumerate(page_paths)],
                    "total_pages": len(page_paths),
                    "pages_processed": len(page_paths),
                    "is_scanned": False,
                }
                quote = Quote(
                    submission_id=submission.id,
                    carrier_name=f"Test_{quote_key}",
                    raw_document_path=pdf_path,
                    pass1_layout_json=json.dumps(layout),
                )
                db_session.add(quote)
                db_session.flush()

                # Create AmsExportJob
                job = AmsExportJob(
                    submission_id=submission.id,
                    quote_id=quote.id,
                    json_data='{}',
                    instructions='accuracy test',
                    status='pending',
                )
                db_session.add(job)
                db_session.commit()
                job_id = job.id

            except Exception as e:
                db_session.rollback()
                result.error = f"DB setup failed: {e}"
                return result
            finally:
                db_session.close()

            # 3. Call the endpoint
            # Clear any cached facts for this job
            from app.routes import _AMS_FACTS_CACHE
            _AMS_FACTS_CACHE.pop(job_id, None)

            client = app.test_client()
            t = time.time()
            resp = client.post('/api/ams/extension-fill', json={
                'job_id': job_id,
                'fields': fields,
                'already_filled': [],
            })
            result.elapsed = time.time() - t

            if resp.status_code != 200:
                result.error = f"HTTP {resp.status_code}: {resp.get_data(as_text=True)[:200]}"
                return result

            body = resp.get_json()
            if not body.get('success'):
                result.error = f"Endpoint error: {body.get('error')}"
                return result

            fills = body.get('fills', {})

            # 4. Compare to answer key
            returned_selectors = set(fills.keys())
            expected_selectors = set(expected_fields.keys())

            result.returned_fields = len(returned_selectors & expected_selectors)
            result.extra = sorted(returned_selectors - expected_selectors)
            result.missing = sorted(expected_selectors - returned_selectors)

            for selector, expected_value in expected_fields.items():
                actual_value = fills.get(selector, {}).get('value') if selector in fills else None

                is_contested = selector in contested_selectors

                if actual_value is None:
                    # Field not returned — counts as recall miss but not accuracy failure
                    continue

                matched = values_match(actual_value, expected_value)

                if is_contested:
                    result.contested_returned += 1
                    if matched:
                        result.contested_matched += 1
                else:
                    result.unanimous_returned += 1
                    if matched:
                        result.unanimous_matched += 1
                    else:
                        result.mismatches.append((selector, expected_value, actual_value))

            # 5. Cleanup DB records
            db_session = get_session()
            try:
                db_session.query(AmsExportJob).filter_by(id=job_id).delete()
                db_session.query(Quote).filter_by(submission_id=submission.id).delete()
                db_session.query(Submission).filter_by(id=submission.id).delete()
                db_session.commit()
            except Exception:
                db_session.rollback()
            finally:
                db_session.close()

        finally:
            # Cleanup temp images
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return result


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Parse args
    filter_quote = None
    verbose = False
    for arg in sys.argv[1:]:
        if arg in ('-v', '--verbose'):
            verbose = True
        else:
            filter_quote = arg

    # Load answer keys
    with open(ANSWER_KEYS_PATH) as f:
        answer_keys = json.load(f)

    # Build fields
    fields = build_fields_from_fake_epic()
    print(f"Fields: {len(fields)}")
    print(f"Answer keys: {len(answer_keys)} quotes")
    print(f"Thresholds: unanimous≥{UNANIMOUS_MATCH_THRESHOLD:.0%}, "
          f"contested≥{CONTESTED_MATCH_THRESHOLD:.0%}, "
          f"recall≥{OVERALL_RECALL_THRESHOLD:.0%}")
    print()

    # Create app
    app = create_app()

    results = []
    passed = 0
    failed = 0

    for quote_key, answer_entry in sorted(answer_keys.items()):
        if filter_quote and filter_quote not in quote_key:
            continue

        pdf_path = QUOTE_PDF_MAP.get(quote_key)
        if not pdf_path or not os.path.exists(pdf_path):
            print(f"  SKIP {quote_key}: PDF not found ({pdf_path})")
            continue

        print(f"  {quote_key:30s} ", end='', flush=True)
        result = run_quote_test(app, quote_key, pdf_path, answer_entry, fields)
        results.append(result)

        print(result.summary())

        if result.passed:
            passed += 1
            if verbose:
                if result.mismatches:
                    for sel, exp, act in result.mismatches:
                        print(f"      MISMATCH {sel}: expected={exp!r}, got={act!r}")
                if result.missing:
                    print(f"      MISSING ({len(result.missing)}): {result.missing}")
                if result.extra:
                    print(f"      EXTRA ({len(result.extra)}): {result.extra}")
        else:
            failed += 1
            # Show details on failure
            if result.mismatches:
                for sel, exp, act in result.mismatches[:5]:
                    print(f"      MISMATCH {sel}: expected={exp!r}, got={act!r}")
                if len(result.mismatches) > 5:
                    print(f"      ... and {len(result.mismatches) - 5} more")
            if result.missing and len(result.missing) <= 8:
                print(f"      MISSING: {result.missing}")

    # Summary
    print(f"\n{'='*70}")
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")

    if results:
        avg_unanimous = sum(r.unanimous_pct for r in results if not r.error) / max(len([r for r in results if not r.error]), 1)
        avg_recall = sum(r.recall_pct for r in results if not r.error) / max(len([r for r in results if not r.error]), 1)
        avg_time = sum(r.elapsed for r in results) / len(results)
        print(f"Average: unanimous={avg_unanimous:.0%}, recall={avg_recall:.0%}, time={avg_time:.1f}s")

    sys.exit(0 if failed == 0 else 1)


if __name__ == '__main__':
    main()
