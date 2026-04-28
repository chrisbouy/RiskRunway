import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.parsers import two_pass_parser
from app.parsers.llm_parsers import BedrockClient
import settings

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOCS_DIR = ROOT / 'sample_docs'
FIXTURES_DIR = ROOT / 'tests' / 'e2e' / 'fixtures' / 'quote-parsing'


def find_pdf_for_fixture(base_name: str) -> Path:
    for suffix in ['.pdf', '.PDF']:
        candidate = SAMPLE_DOCS_DIR / f'{base_name}{suffix}'
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f'Could not find PDF for fixture {base_name} in {SAMPLE_DOCS_DIR}'
    )

class TestTwoPassParserBedrock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Ensure the parser uses Bedrock for this test run.
        cls.patch_provider = patch.object(settings, 'LLM_PROVIDER', 'bedrock')
        cls.patch_provider.start()
        cls.bedrock_client = BedrockClient()

    @classmethod
    def tearDownClass(cls):
        cls.patch_provider.stop()

    def test_all_sample_docs_parse_against_expected_fixtures(self):
        fixture_files = sorted(FIXTURES_DIR.glob('*.json'))
        if not fixture_files:
            self.skipTest(f'No fixtures found in {FIXTURES_DIR}')

        for fixture_file in fixture_files:
            base_name = fixture_file.stem
            pdf_path = find_pdf_for_fixture(base_name)
            with self.subTest(pdf=pdf_path.name):
                expected = json.loads(fixture_file.read_text(encoding='utf-8'))
                result = two_pass_parser.process_quote_two_pass(str(pdf_path))
                self.assertIn('pass2_normalized', result)
                actual = result['pass2_normalized']
                self.maxDiff = None
                if actual != expected:
                    print(f"\n=== Parsed output mismatch for {pdf_path.name} ===")
                    print("EXPECTED:")
                    print(json.dumps(expected, indent=2))
                    print("ACTUAL:")
                    print(json.dumps(actual, indent=2))
                self.assertEqual(
                    actual,
                    expected,
                    f'Parsed output for {pdf_path.name} did not match expected fixture'
                )


if __name__ == '__main__':
    unittest.main()
