import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.parsers import two_pass_parser

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOCS_DIR = ROOT / 'sample_docs'


class TestTwoPassParser(unittest.TestCase):
    def test_pass2_normalize_quote_data_uses_llm_client(self):
        layout_data = {
            "pages": [
                {
                    "page_number": 1,
                    "text": "Applicant: Test Corp\nPolicy Number: ABC123"
                }
            ]
        }

        expected_normalized = {
            "insured": {
                "name": "Test Corp",
                "address": {
                    "street": "123 Main St",
                    "city": "Testville",
                    "state": "TX",
                    "zip": "75001"
                }
            },
            "retail_agent": {
                "name": None,
                "code": None,
                "address": {
                    "street": None,
                    "city": None,
                    "state": None,
                    "zip": None
                },
                "phone": None
            },
            "general_agent_or_wholesale_broker": {
                "name": None,
                "address": {
                    "street": None,
                    "city": None,
                    "state": None,
                    "zip": None
                },
                "phone": None
            },
            "quote_number": "ABC123",
            "account_number": None,
            "policies": [],
            "totals": {
                "total_premium": None,
                "total_tax": None,
                "total_fee": None,
                "total_broker_fee": None,
                "grand_total": None
            },
            "financing": {
                "down_payment": None,
                "amount_financed": None
            }
        }

        mock_llm = Mock()
        mock_llm.generate_json.return_value = expected_normalized

        with patch.object(two_pass_parser, 'get_llm_client', return_value=mock_llm):
            normalized = two_pass_parser.pass2_normalize_quote_data(layout_data)

        self.assertEqual(normalized, expected_normalized)
        mock_llm.generate_json.assert_called_once()

        prompt_text = mock_llm.generate_json.call_args.args[0]
        self.assertIn('Extracted Layout Data:', prompt_text)
        self.assertIn(json.dumps(layout_data), prompt_text)

    def test_process_quote_two_pass_calls_pass1_and_pass2(self):
        fake_pdf_path = SAMPLE_DOCS_DIR / 'quote_frogA.pdf'
        fake_layout = {
            "pages": [
                {
                    "page_number": 1,
                    "text": "Applicant: Test Corp"
                }
            ]
        }
        fake_normalized = {
            "insured": {"name": "Test Corp", "address": {"street": None, "city": None, "state": None, "zip": None}},
            "retail_agent": {"name": None, "code": None, "address": {"street": None, "city": None, "state": None, "zip": None}, "phone": None},
            "general_agent_or_wholesale_broker": {"name": None, "address": {"street": None, "city": None, "state": None, "zip": None}, "phone": None},
            "quote_number": None,
            "account_number": None,
            "policies": [],
            "totals": {"total_premium": None, "total_tax": None, "total_fee": None, "total_broker_fee": None, "grand_total": None},
            "financing": {"down_payment": None, "amount_financed": None}
        }

        mock_llm = Mock()
        mock_llm.generate_json.return_value = fake_normalized

        with patch.object(two_pass_parser, 'pass1_extract_quote_layout', return_value=fake_layout) as mock_pass1, \
             patch.object(two_pass_parser, 'get_llm_client', return_value=mock_llm):
            result = two_pass_parser.process_quote_two_pass(str(fake_pdf_path))

        mock_pass1.assert_called_once_with(str(fake_pdf_path))
        self.assertEqual(result['pass1_layout'], fake_layout)
        self.assertEqual(result['pass2_normalized'], fake_normalized)


if __name__ == '__main__':
    unittest.main()
