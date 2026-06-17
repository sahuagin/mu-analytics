import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fixtures  # noqa: E402

import panels  # noqa: E402


class TestPanels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = fixtures.fixture_connection(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tool_mix(self):
        self.assertEqual(
            {r["tool"]: r["count"] for r in panels.tool_mix(self.con)},
            {"bash": 1, "read": 1},
        )

    def test_recall(self):
        rc = {r["source"]: r["tokens"] for r in panels.recall(self.con)}
        self.assertEqual(rc["ProjectFile"], 5678)
        self.assertEqual(rc["Memory"], 1234)

    def test_compaction(self):
        mu = panels.compaction(self.con)["mu"]
        self.assertEqual(mu["dropped"], 1)
        self.assertEqual(mu["kept"], 0)
        self.assertEqual(mu["before"], 100000)
        self.assertEqual(mu["after"], 20000)
        self.assertEqual(mu["events"], 1)

    def test_stop_reason_and_degradation(self):
        self.assertEqual(
            {r["stop_reason"]: r["count"] for r in panels.stop_reason_health(self.con)},
            {"degraded_eof": 1},
        )
        self.assertEqual(panels.degradation_rate(self.con), 100.0)  # 1/1 non-clean
        self.assertEqual(list(panels.degradation_by_day(self.con).values()), [1.0])

    def test_context_trajectory(self):
        traj, drops, daemon = panels.context_trajectory(self.con)
        self.assertEqual(daemon, "testdaemon01")
        self.assertEqual(traj, [12])  # 12000 tokens -> 12k
        self.assertEqual(drops, [])

    def test_cache_econ(self):
        ce = panels.cache_econ(self.con)
        self.assertEqual(ce["w5_tokens"], 500)
        self.assertEqual(ce["w1_tokens"], 1500)
        for k in ("median_gap_min", "p90_gap_min", "save_pct", "save_pct_p90"):
            self.assertIn(k, ce)

    def test_per_ask(self):
        pa = panels.per_ask(self.con)  # default-daemon path falls back on a small corpus
        self.assertEqual(pa["model"], "claude-opus-4-8")
        self.assertEqual(len(pa["asks"]), 1)
        ask = pa["asks"][0]
        self.assertGreater(ask["cost"], 0)
        self.assertTrue(ask["rewrite_5m"])  # cache_creation 2000 > 0

    def test_flagged_queue(self):
        q = panels.flagged_queue(self.con)
        reasons = {x["reason"] for x in q}
        self.assertIn("deg", reasons)  # done.stop_reason=degraded_eof
        self.assertIn("err", reasons)  # task_telemetry.exit_reason=error
        self.assertIn("callout", reasons)  # callout event
        self.assertTrue(all(x["id"].startswith("mu·") for x in q))
        self.assertTrue(all(x["fleet"] == "mu" for x in q))


class TestTranscripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = fixtures.transcript_connection(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_keys_match_session_display_ids(self):
        tx = panels.session_transcripts(self.con)
        # mu sessions key on "<daemon>/<session_id>"; cc on the task_telemetry.task_id
        self.assertIn(("mu", "txdaemon/s1"), tx)
        self.assertIn(("cc", "cc-txcc-0000-1111"), tx)

    def test_mu_turn_reconstruction_skips_empty_and_pure_tooluse(self):
        turns = panels.session_transcripts(self.con)[("mu", "txdaemon/s1")]
        # event 3 (pure tool-use assistant) and event 7 (blank user) are dropped
        self.assertEqual([t[0] for t in turns], ["u", "a", "t", "t", "t", "u"])
        # the assistant turn is the real text, NOT a dumped tool_call JSON array
        a = next(t for t in turns if t[0] == "a")
        self.assertEqual(a[2], "Looking at it now.")
        self.assertFalse(a[2].lstrip().startswith("[{"))
        # tool_result turns carry the error/ok label + a content snippet
        results = [t for t in turns if t[1].startswith("→")]
        self.assertTrue(any(t[1].startswith("→ error") and t[2] == "boom" for t in results))
        self.assertTrue(any(t[1].startswith("→ result") for t in results))

    def test_cc_turn_reconstruction(self):
        turns = panels.session_transcripts(self.con)[("cc", "cc-txcc-0000-1111")]
        self.assertEqual([t[0] for t in turns], ["u", "a", "t", "t"])
        self.assertEqual(next(t for t in turns if t[0] == "a")[2], "on it")
        self.assertIn("127.0.0.1 localhost", turns[-1][2])

    def test_write_session_transcripts_sidecars(self):
        import json

        from sample_data import _short_id

        sdir = os.path.join(self.tmp.name, "out", "sessions")
        stats = panels.write_session_transcripts(self.con, sdir)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["written"], 2)
        # a sidecar per session, named by the display-id slug (· -> -), holding the turns
        mu_file = os.path.join(sdir, panels._slug(_short_id("mu", "txdaemon/s1")) + ".json")
        self.assertTrue(os.path.exists(mu_file))
        with open(mu_file) as f:
            turns = json.load(f)
        self.assertEqual([t[0] for t in turns], ["u", "a", "t", "t", "t", "u"])
        self.assertTrue(os.path.exists(os.path.join(sdir, "_manifest.json")))

    def test_write_session_transcripts_skips_unchanged(self):
        sdir = os.path.join(self.tmp.name, "out2", "sessions")
        panels.write_session_transcripts(self.con, sdir)
        again = panels.write_session_transcripts(self.con, sdir)  # nothing changed
        self.assertEqual(again["written"], 0)
        self.assertEqual(again["total"], 2)


if __name__ == "__main__":
    unittest.main()
