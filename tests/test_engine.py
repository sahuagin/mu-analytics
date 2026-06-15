import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fixtures  # noqa: E402

import engine  # noqa: E402


class TestEngine(unittest.TestCase):
    def test_events_present(self):
        with tempfile.TemporaryDirectory() as d:
            orig_mu, orig_cc = engine.MU_EVENTS, engine.CC_EVENTS
            try:
                # point both fleets at absent dirs so the check is hermetic
                engine.MU_EVENTS = os.path.join(d, "events")
                engine.CC_EVENTS = os.path.join(d, "cc-events")
                self.assertFalse(engine.events_present())  # both dirs absent
                fixtures.write_event_log(d)
                self.assertTrue(engine.events_present())  # mu log present
            finally:
                engine.MU_EVENTS, engine.CC_EVENTS = orig_mu, orig_cc

    def test_cc_present_alone_counts(self):
        # cc events alone (no mu) still register as "present"
        with tempfile.TemporaryDirectory() as d:
            orig_mu, orig_cc = engine.MU_EVENTS, engine.CC_EVENTS
            try:
                engine.MU_EVENTS = os.path.join(d, "events")  # absent
                engine.CC_EVENTS = os.path.join(d, "cc-events")
                self.assertFalse(engine.events_present())
                fixtures.write_cc_event_log(d)
                self.assertTrue(engine.events_present())
            finally:
                engine.MU_EVENTS, engine.CC_EVENTS = orig_mu, orig_cc

    def test_both_fleets_union_and_tags(self):
        # WS3: the ev view unions both fleets, tags each row's fleet, and keys
        # cc rows by session_id (not the collapsed "claude-code" provider dir).
        with tempfile.TemporaryDirectory() as d:
            con = fixtures.both_fleets_connection(d)
            by_fleet = dict(con.execute("SELECT fleet, count(*) FROM ev GROUP BY fleet").fetchall())
            self.assertEqual(set(by_fleet), {"mu", "cc"})
            self.assertGreater(by_fleet["mu"], 0)
            self.assertGreater(by_fleet["cc"], 0)

            # cc carries the rich behavioral kinds (the whole point of WS2+WS3)
            cc_kinds = {
                k
                for (k,) in con.execute("SELECT DISTINCT kind FROM ev WHERE fleet='cc'").fetchall()
            }
            for rich in ("user_message", "assistant_message_event", "tool_result", "done"):
                self.assertIn(rich, cc_kinds)

            # cc per-session key = session_id (unique), not the provider dir name
            cc_daemons = {
                dn
                for (dn,) in con.execute(
                    "SELECT DISTINCT daemon FROM ev WHERE fleet='cc'"
                ).fetchall()
            }
            self.assertEqual(cc_daemons, {fixtures._CC_SESSION})
            self.assertNotIn("claude-code", cc_daemons)

            # mu rows are keyed by their per-session dir, as before
            mu_daemons = {
                dn
                for (dn,) in con.execute(
                    "SELECT DISTINCT daemon FROM ev WHERE fleet='mu'"
                ).fetchall()
            }
            self.assertEqual(mu_daemons, {"testdaemon01"})

    def test_ev_view_and_histogram(self):
        with tempfile.TemporaryDirectory() as d:
            con = fixtures.fixture_connection(d)
            hist = dict(engine.histogram(con))
            self.assertEqual(hist["tool_call"], 2)
            self.assertEqual(hist["tool_result"], 2)
            self.assertEqual(hist["done"], 1)
            self.assertEqual(hist["operator_mark"], 1)

    def test_daemon_extracted_from_path(self):
        with tempfile.TemporaryDirectory() as d:
            con = fixtures.fixture_connection(d)
            daemons = con.execute("SELECT DISTINCT daemon FROM ev").fetchall()
            self.assertEqual(daemons, [("testdaemon01",)])

    def test_payload_is_json_not_kind_only_map(self):
        # the whole point of pinning payload:JSON — inner keys must resolve
        with tempfile.TemporaryDirectory() as d:
            con = fixtures.fixture_connection(d)
            v = con.execute(
                "SELECT json_extract(payload,'$.token_count_estimate')::BIGINT "
                "FROM ev WHERE kind='context_assembly'"
            ).fetchone()[0]
            self.assertEqual(v, 12000)


if __name__ == "__main__":
    unittest.main()
