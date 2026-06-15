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
            orig = engine.MU_EVENTS
            try:
                engine.MU_EVENTS = os.path.join(d, "events")
                self.assertFalse(engine.events_present())  # dir absent
                fixtures.write_event_log(d)
                self.assertTrue(engine.events_present())  # log present
            finally:
                engine.MU_EVENTS = orig

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
