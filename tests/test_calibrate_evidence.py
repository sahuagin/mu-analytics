import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calibrate_evidence import CASES, case_inputs, main, write_result


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

    def test_errors_fail_run_and_resume_without_silent_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = ["calibrate", "--out", directory, "--require-provider", "local", "--limit", "1"]
            with (
                patch("sys.argv", argv),
                patch("calibrate_evidence.pw.resolve_target", return_value="local/test"),
                patch(
                    "calibrate_evidence.pw.judge_chunk", side_effect=RuntimeError("timeout")
                ) as judge,
                patch("builtins.print"),
            ):
                self.assertEqual(main(), 1)
                self.assertEqual(judge.call_count, 2)
                files = {p.name: p.read_bytes() for p in Path(directory).glob("*.json")}
                self.assertEqual(main(), 1)
                self.assertEqual(judge.call_count, 2)
                self.assertEqual(
                    files, {p.name: p.read_bytes() for p in Path(directory).glob("*.json")}
                )

    def test_atomic_result_failure_preserves_previous_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            write_result(path, {"status": "old"})
            with patch("calibrate_evidence.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    write_result(path, {"status": "new"})
            self.assertEqual(json.loads(path.read_text()), {"status": "old"})
            self.assertEqual(list(Path(directory).iterdir()), [path])

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
