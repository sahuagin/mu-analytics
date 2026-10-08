import json
import os
import sqlite3
import tempfile
import unittest

import fixtures

import pointwise_judge as pw


class TestPointwiseJudge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = fixtures.transcript_connection(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_one_session_from_existing_projection(self):
        turns = pw.load_turns(self.con, "mu", "txdaemon/s1")
        self.assertEqual(
            [t.kind for t in turns],
            [
                "user_message",
                "assistant_message_event",
                "tool_call",
                "tool_result",
                "tool_result",
                "user_message",
            ],
        )
        self.assertEqual([t.number for t in turns], list(range(1, 7)))
        self.assertNotIn("do the cc thing", "\n".join(t.body for t in turns))

    def test_chunks_are_bounded_and_keep_turn_labels(self):
        turns = pw.load_turns(self.con, "mu", "txdaemon/s1")
        chunks = pw.chunk_turns(turns, max_chunk_bytes=90, max_tool_chars=20)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.text.encode()) <= 90 for c in chunks))
        joined = "\n".join(c.text for c in chunks)
        self.assertIn("[001] USER:", joined)
        self.assertIn("TOOL_CALL(bash)", joined)
        self.assertIn("TOOL_RESULT(err)", joined)

    def test_oversized_utf8_turn_is_split_without_exceeding_bound(self):
        turn = pw.Turn(7, 1, "user_message", None, None, "é" * 100)
        chunks = pw.chunk_turns([turn], max_chunk_bytes=72)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.text.encode()) <= 72 for c in chunks))
        self.assertTrue(all(c.text.encode().decode() == c.text for c in chunks))

    def test_commits_each_chunk_and_rerun_skips_it(self):
        calls = []

        def fake_judge(chunk, cls, role, required_provider, timeout):
            calls.append(chunk.index)
            return {
                "behavior": cls,
                "occurred": False,
                "severity": "low",
                "confidence": "high",
                "evidence": [],
                "summary": "synthetic",
                "judge_model": "flashnext/test-model",
            }

        def target(_role, provider):
            self.assertEqual(provider, "flashnext")
            return "flashnext/test-model"

        db = os.path.join(self.tmp.name, "pointwise.sqlite")
        store = pw.ResultStore(db)
        try:
            first = pw.process_session(
                self.con,
                "mu",
                "txdaemon/s1",
                "false_success",
                "flashnext",
                store,
                max_chunk_bytes=90,
                max_tool_chars=20,
                timeout=1,
                judge=fake_judge,
                target_resolver=target,
            )
            first_calls = list(calls)
            second = pw.process_session(
                self.con,
                "mu",
                "txdaemon/s1",
                "false_success",
                "flashnext",
                store,
                max_chunk_bytes=90,
                max_tool_chars=20,
                timeout=1,
                judge=fake_judge,
                target_resolver=target,
            )
        finally:
            store.close()

        self.assertEqual(first["judged"], first["chunks"])
        self.assertEqual(second["judged"], 0)
        self.assertEqual(second["already_done"], first["chunks"])
        self.assertEqual(calls, first_calls)
        con = sqlite3.connect(db)
        try:
            rows = con.execute(
                "SELECT chunk_index, target, verdict_json FROM pointwise_result ORDER BY chunk_index"
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(len(rows), first["chunks"])
        self.assertTrue(all(row[1] == "flashnext/test-model" for row in rows))
        self.assertTrue(all(json.loads(row[2])["occurred"] is False for row in rows))


if __name__ == "__main__":
    unittest.main()
