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
        turns = pw.load_turns(self.con, "mu", "txdaemon:s1")
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

    def test_uses_engine_canonical_session_key_for_both_fleets(self):
        cc = pw.load_turns(self.con, "cc", "txcc-0000-1111")
        self.assertEqual(
            [t.kind for t in cc],
            [
                "user_message",
                "assistant_message_event",
                "tool_call",
                "tool_result",
            ],
        )
        self.assertEqual(pw.load_turns(self.con, "cc", "cc-txcc-0000-1111"), [])

    def test_plan_contains_only_aggregate_chunk_metadata(self):
        _turns, _chunks, plan = pw.session_plan(self.con, "mu", "txdaemon:s1", 90, 20)
        self.assertGreater(plan["chunks"], 1)
        self.assertLessEqual(plan["chunk_bytes_max"], 90)
        self.assertEqual(
            set(plan),
            {
                "session_ref",
                "turns",
                "chunks",
                "chunk_bytes_total",
                "chunk_bytes_min",
                "chunk_bytes_max",
            },
        )
        self.assertNotIn("please fix", json.dumps(plan))

    def test_chunks_are_bounded_and_keep_turn_labels(self):
        turns = pw.load_turns(self.con, "mu", "txdaemon:s1")
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

        def fake_judge(chunk, cls, role, required_provider, timeout, verification_retry=False):
            calls.append((chunk.index, cls, verification_retry))
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
                "txdaemon:s1",
                "false_success",
                "flashnext",
                store,
                max_chunk_bytes=90,
                max_tool_chars=20,
                timeout=1,
                limit_chunks=1,
                judge=fake_judge,
                target_resolver=target,
            )
            second = pw.process_session(
                self.con,
                "mu",
                "txdaemon:s1",
                "false_success",
                "flashnext",
                store,
                max_chunk_bytes=90,
                max_tool_chars=20,
                timeout=1,
                judge=fake_judge,
                target_resolver=target,
            )
            second_calls = list(calls)
            third = pw.process_session(
                self.con,
                "mu",
                "txdaemon:s1",
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

        self.assertEqual(first["accepted"], 1)
        self.assertEqual(first["model_calls"], 1)
        self.assertEqual(first["completed_before"], 0)
        self.assertEqual(first["remaining"], first["chunks"] - 1)
        self.assertEqual(second["accepted"], first["chunks"] - 1)
        self.assertEqual(second["completed_before"], 1)
        self.assertEqual(second["remaining"], 0)
        self.assertEqual(third["accepted"], 0)
        self.assertEqual(third["completed_before"], first["chunks"])
        self.assertEqual(third["remaining"], 0)
        self.assertEqual(calls, second_calls)
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

    def test_profile_runs_classes_inside_each_selected_chunk(self):
        calls = []

        def fake(chunk, cls, role, provider, timeout, retry=False):
            calls.append((chunk.index, cls, retry))
            return {
                "behavior": cls,
                "occurred": False,
                "evidence": [],
                "judge_model": "flashnext/m",
            }

        store = pw.ResultStore(os.path.join(self.tmp.name, "profile.sqlite"))
        try:
            stats = pw.process_session(
                self.con,
                "mu",
                "txdaemon:s1",
                "false_success",
                "flashnext",
                store,
                90,
                20,
                1,
                limit_chunks=1,
                judge=fake,
                target_resolver=lambda _r, _p: "flashnext/m",
                pointwise_profile=True,
            )
        finally:
            store.close()
        self.assertEqual([c[1] for c in calls], list(pw.POINTWISE_CLASSES))
        self.assertTrue(all(c[0] == 0 and c[2] is False for c in calls))
        self.assertEqual(stats["accepted"], len(pw.POINTWISE_CLASSES))
        self.assertEqual(stats["analyzers"], len(pw.POINTWISE_CLASSES) + 1)
        self.assertEqual(stats["units_total"], stats["chunks"] * len(pw.POINTWISE_CLASSES) + 1)

    def test_positive_unverified_evidence_retries_then_accepts(self):
        calls = []

        def fake(chunk, cls, role, provider, timeout, retry=False):
            calls.append(retry)
            return {
                "behavior": cls,
                "occurred": True,
                "evidence": [{"quote": "a"}] if retry else [{"quote": "a"}, {"quote": "b"}],
                "n_evidence_verified": 1,
                "judge_model": "flashnext/m",
            }

        db = os.path.join(self.tmp.name, "retry.sqlite")
        store = pw.ResultStore(db)
        try:
            stats = pw.process_session(
                self.con,
                "mu",
                "txdaemon:s1",
                "false_success",
                "flashnext",
                store,
                8192,
                20,
                1,
                judge=fake,
                target_resolver=lambda _r, _p: "flashnext/m",
            )
        finally:
            store.close()
        self.assertEqual(calls, [False, True])
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["model_calls"], 2)
        raw = sqlite3.connect(db).execute("SELECT verdict_json FROM pointwise_result").fetchone()[0]
        verdict = json.loads(raw)
        self.assertEqual(verdict["verification_disposition"], "accepted_after_retry")
        self.assertEqual(verdict["initial_unverified_count"], 1)

    def test_failed_positive_retry_is_local_quarantine_and_terminal(self):
        calls = []

        def fake(chunk, cls, role, provider, timeout, retry=False):
            calls.append(retry)
            return {
                "behavior": cls,
                "occurred": True,
                "evidence": [{"quote": "a"}, {"quote": "b"}],
                "n_evidence_verified": 1,
                "judge_model": "flashnext/m",
            }

        db = os.path.join(self.tmp.name, "quarantine.sqlite")
        store = pw.ResultStore(db)
        try:
            first = pw.process_session(
                self.con,
                "mu",
                "txdaemon:s1",
                "false_success",
                "flashnext",
                store,
                8192,
                20,
                1,
                judge=fake,
                target_resolver=lambda _r, _p: "flashnext/m",
            )
            second = pw.process_session(
                self.con,
                "mu",
                "txdaemon:s1",
                "false_success",
                "flashnext",
                store,
                8192,
                20,
                1,
                judge=fake,
                target_resolver=lambda _r, _p: "flashnext/m",
            )
        finally:
            store.close()
        self.assertEqual(calls, [False, True])
        self.assertEqual(first["quarantined"], 1)
        self.assertEqual(first["accepted"], 0)
        self.assertEqual(second["model_calls"], 0)
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "SELECT category, reason_json, egress_allowed FROM pointwise_quarantine"
            ).fetchone()
            accepted = con.execute("SELECT count(*) FROM pointwise_result").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(accepted, 0)
        self.assertEqual(row[0], "positive_unverified_evidence_after_retry")
        self.assertEqual(json.loads(row[1])["egress_default"], "deny")
        self.assertEqual(row[2], 0)


if __name__ == "__main__":
    unittest.main()
