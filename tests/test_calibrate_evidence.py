import json
import unittest

from calibrate_evidence import CASES, case_inputs


class TestSyntheticEvidenceCases(unittest.TestCase):
    def test_fixed_cases_have_labels_and_paired_inputs(self):
        cases = json.loads(CASES.read_text())
        self.assertEqual(len(cases), 8)
        self.assertEqual(len({c["id"] for c in cases}), 8)
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertIsInstance(case["expected"], bool)
                inputs = case_inputs(case)
                self.assertIn(inputs["transcript_only"], inputs["structured_evidence"])
                self.assertNotIn(case["reason"], inputs["structured_evidence"])
                self.assertIn(case["claim"], inputs["transcript_only"])
                self.assertIn("event_id < claim_event_id", inputs["structured_evidence"])

    def test_completeness_is_explicit_in_both_fixture_modes(self):
        case = json.loads(CASES.read_text())[0]
        inputs = case_inputs(case)
        for text in inputs.values():
            self.assertIn("no earlier or intervening events are omitted", text)
        self.assertIn("complete synthetic sequence before the claim", inputs["structured_evidence"])
        self.assertNotIn("absence is not proof of non-execution", inputs["structured_evidence"])

    def test_nonerror_fixture_is_only_a_launch(self):
        case = next(
            c for c in json.loads(CASES.read_text()) if c["id"] == "nonerror_is_not_completion"
        )
        inputs = case_inputs(case)
        self.assertIn('"record_status": "result_reports_non_error"', inputs["structured_evidence"])
        self.assertIn("tests are still running", inputs["structured_evidence"])
        self.assertTrue(case["expected"])


if __name__ == "__main__":
    unittest.main()
