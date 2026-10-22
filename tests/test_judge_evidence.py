import json
import unittest

import duckdb

from judge_evidence import collect_evidence, render_evidence


class TestRecordedEvidence(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect()
        self.con.execute(
            "CREATE TABLE ev (id BIGINT, ts BIGINT, kind VARCHAR, payload JSON, fleet VARCHAR, session VARCHAR, filename VARCHAR)"
        )
        self.add(10, "assistant_message_event", {})

    def tearDown(self):
        self.con.close()

    def add(self, event_id, kind, payload, *, fleet="mu", session="s", filename="source"):
        self.con.execute(
            "INSERT INTO ev VALUES (?, ?, ?, ?, ?, ?, ?)",
            [event_id, event_id * 100, kind, json.dumps(payload), fleet, session, filename],
        )

    def get(self, **kwargs):
        return collect_evidence(self.con, "mu", "s", 10, **kwargs)

    def test_call_is_attempt_and_later_result_is_not_claim_time_evidence(self):
        self.add(
            2, "tool_call", {"call_id": "a", "name": "shell", "arguments": {"command": "test"}}
        )
        self.add(11, "tool_result", {"call_id": "a", "content": "passed", "is_error": False})
        self.assertEqual(self.get()["tools"][0]["record_status"], "call_without_recorded_result")

    def test_complete_error_result_and_scope_isolation(self):
        self.add(1, "tool_call", {"call_id": "a", "name": "shell"})
        text = "x" * 5000 + " explicit failure"
        self.add(2, "tool_result", {"call_id": "a", "content": text, "is_error": True})
        self.add(
            2,
            "tool_result",
            {"call_id": "a", "content": "passed", "is_error": False},
            session="other",
        )
        packet = self.get()
        self.assertEqual(packet["tools"][0]["record_status"], "result_reports_error")
        self.assertIn(text, render_evidence(packet))
        self.assertNotIn("passed", render_evidence(packet))

    def test_nonerror_is_not_labeled_success(self):
        self.add(1, "tool_call", {"call_id": "a"})
        self.add(2, "tool_result", {"call_id": "a", "content": "started", "is_error": False})
        self.assertEqual(self.get()["tools"][0]["record_status"], "result_reports_non_error")

    def test_duplicate_call_id_is_ambiguous(self):
        self.add(1, "tool_call", {"call_id": "a"})
        self.add(2, "tool_call", {"call_id": "a"})
        self.add(3, "tool_result", {"call_id": "a"})
        self.assertEqual(self.get()["tools"][0]["record_status"], "ambiguous_reused_id")

    def test_multiple_sources_require_selection(self):
        self.add(10, "assistant_message_event", {}, filename="copy")
        with self.assertRaises(ValueError):
            self.get()
        self.assertEqual(self.get(filename="source")["tools"], [])

    def test_orphans_and_explicit_selection(self):
        self.add(2, "tool_result", {"call_id": "orphan", "content": "result"})
        self.add(3, "tool_call", {"name": "unknown"})
        packet = self.get()
        self.assertEqual(packet["tools"][0]["record_status"], "result_without_recorded_call")
        self.assertEqual(len(packet["unlinked_records"]), 1)
        packet = self.get(call_ids=["missing"])
        self.assertEqual(packet["requested_ids_without_prior_records"], ["missing"])
        self.assertEqual(packet["tools"], [])

    def test_selection_reports_omitted_followup_without_future_leakage(self):
        self.add(1, "tool_call", {"call_id": "launch"})
        self.add(2, "tool_result", {"call_id": "launch", "is_error": True, "content": "timeout"})
        self.add(3, "tool_call", {"call_id": "check"})
        self.add(4, "tool_result", {"call_id": "check", "content": "background job passed"})
        self.add(11, "tool_result", {"call_id": "future", "content": "later evidence"})
        self.add(5, "tool_result", {"call_id": "other"}, session="other")
        packet = self.get(call_ids=["launch"])
        self.assertEqual(
            packet["selection_coverage"],
            {
                "prior_tool_records_in_source": 4,
                "included_tool_records": 2,
                "omitted_tool_records": 2,
                "all_prior_recorded_tools_included": False,
                "source_completeness": "unknown",
            },
        )
        self.assertNotIn("background job passed", render_evidence(packet))
        full = self.get()
        self.assertIn("background job passed", render_evidence(full))
        self.assertNotIn("later evidence", render_evidence(full))
        self.assertTrue(full["selection_coverage"]["all_prior_recorded_tools_included"])
        self.assertEqual(full["selection_coverage"]["source_completeness"], "unknown")

    def test_empty_selection_reports_omissions_including_unlinked_records(self):
        self.add(1, "tool_call", {"name": "unknown"})
        self.add(2, "tool_result", {"call_id": "a"})
        packet = self.get(call_ids=[])
        self.assertEqual(packet["selection_coverage"]["omitted_tool_records"], 2)
        self.assertEqual(packet["selection_coverage"]["included_tool_records"], 0)

    def test_recording_claim_is_not_verifying_claim(self):
        claim = "all tests passed"
        self.add(
            1,
            "tool_call",
            {"call_id": "write", "name": "memory_update", "arguments": {"content": claim}},
        )
        self.add(2, "tool_result", {"call_id": "write", "content": claim, "is_error": False})
        packet = self.get()
        self.assertEqual(packet["tools"][0]["record_status"], "result_reports_non_error")
        self.assertIn("not independent verification", packet["interpretation"])
        self.assertIn(
            "A successful write confirms the write, not the truth", packet["interpretation"]
        )
        self.assertIn(claim, render_evidence(packet))

    def test_reject_nonassistant_claim_and_duplicate_event_ids(self):
        with self.assertRaises(ValueError):
            collect_evidence(self.con, "mu", "s", 99)
        self.add(1, "tool_call", {"call_id": "a"})
        self.add(1, "tool_call", {"call_id": "b"})
        with self.assertRaises(ValueError):
            self.get()


if __name__ == "__main__":
    unittest.main()
