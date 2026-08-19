"""Parquet ev-snapshot tests (mu-mucm.9/.10): incremental refresh + connect()."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fixtures  # noqa: E402

import engine  # noqa: E402


class SnapshotBase(unittest.TestCase):
    """Point the snapshot paths and default sources at a tmpdir, restore after."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        self.glob = fixtures.write_event_log(d)
        self.log = os.path.join(d, "events", "testdaemon01", "session-1.jsonl")
        self.srcs = [(self.glob, "mu", engine._MU_DAEMON, engine._MU_SESSION)]
        self._saved = (
            engine._SNAPSHOT,
            engine._MANIFEST,
            engine._DEFAULT_SOURCES,
            engine._DEFAULT_CON,
        )
        engine._SNAPSHOT = os.path.join(d, "data", "ev-snapshot.parquet")
        engine._MANIFEST = engine._SNAPSHOT + ".manifest.json"
        engine._DEFAULT_SOURCES = self.srcs
        engine._DEFAULT_CON = None

    def tearDown(self):
        (
            engine._SNAPSHOT,
            engine._MANIFEST,
            engine._DEFAULT_SOURCES,
            engine._DEFAULT_CON,
        ) = self._saved
        self._tmp.cleanup()


class TestSnapshotRefresh(SnapshotBase):
    def _snapshot_count(self):
        import duckdb

        return (
            duckdb.execute(f"SELECT count(*) FROM read_parquet('{engine._SNAPSHOT}')").fetchone()
            or (0,)
        )[0]

    def test_initial_build_then_noop(self):
        s = engine.snapshot_refresh(self.srcs)
        self.assertTrue(s["rebuilt"])
        self.assertEqual(s["parsed"], 1)
        self.assertTrue(os.path.exists(engine._SNAPSHOT))
        n = self._snapshot_count()
        self.assertGreater(n, 0)

        s2 = engine.snapshot_refresh(self.srcs)
        self.assertFalse(s2["rebuilt"])
        self.assertEqual(s2["parsed"], 0)
        self.assertEqual(s2["unchanged"], 1)
        self.assertEqual(self._snapshot_count(), n)

    def test_rewritten_identical_content_is_not_reparsed(self):
        engine.snapshot_refresh(self.srcs)
        # rewrite the log byte-identical with a new mtime (what cc emit does)
        with open(self.log, "rb") as f:
            body = f.read()
        with open(self.log, "wb") as f:
            f.write(body)
        os.utime(self.log, ns=(1, 1))
        s = engine.snapshot_refresh(self.srcs)
        self.assertFalse(s["rebuilt"])
        self.assertEqual(s["parsed"], 0)
        # and the manifest absorbed the new mtime — the next run skips the hash
        with open(engine._MANIFEST, encoding="utf-8") as f:
            m = json.load(f)
        self.assertEqual(m[self.log]["mtime_ns"], 1)

    def test_appended_file_is_reparsed(self):
        engine.snapshot_refresh(self.srcs)
        before = self._snapshot_count()
        with open(self.log, "a") as f:
            f.write(
                json.dumps(
                    {
                        "id": 999,
                        "session_id": "session-1",
                        "timestamp_unix_ms": 1_700_009_999_000,
                        "actor": "agent",
                        "payload": {"kind": "tool_call", "name": "extra", "call_id": "c9"},
                    }
                )
                + "\n"
            )
        s = engine.snapshot_refresh(self.srcs)
        self.assertTrue(s["rebuilt"])
        self.assertEqual(s["parsed"], 1)
        self.assertEqual(self._snapshot_count(), before + 1)

    def test_removed_file_rows_are_dropped(self):
        # second daemon so the snapshot survives with rows from the first
        d2 = os.path.join(os.path.dirname(os.path.dirname(self.log)), "testdaemon02")
        os.makedirs(d2)
        with open(os.path.join(d2, "session-1.jsonl"), "w") as f:
            f.write(
                json.dumps(
                    {
                        "id": 1,
                        "session_id": "session-1",
                        "timestamp_unix_ms": 1_700_000_000_000,
                        "actor": "agent",
                        "payload": {"kind": "session_created"},
                    }
                )
                + "\n"
            )
        engine.snapshot_refresh(self.srcs)
        os.remove(os.path.join(d2, "session-1.jsonl"))
        os.rmdir(d2)
        s = engine.snapshot_refresh(self.srcs)
        self.assertTrue(s["rebuilt"])
        self.assertEqual(s["removed"], 1)
        import duckdb

        daemons = {
            dn
            for (dn,) in duckdb.execute(
                f"SELECT DISTINCT daemon FROM read_parquet('{engine._SNAPSHOT}')"
            ).fetchall()
        }
        self.assertEqual(daemons, {"testdaemon01"})


class TestConnectViaSnapshot(SnapshotBase):
    def test_default_connect_serves_ev_from_snapshot(self):
        con = engine.connect()
        self.assertTrue(os.path.exists(engine._SNAPSHOT))
        hist = dict(engine.histogram(con))
        self.assertEqual(hist["tool_call"], 2)
        # payload inner keys must still resolve through the parquet round-trip
        v = (
            con.execute(
                "SELECT json_extract(payload,'$.token_count_estimate')::BIGINT "
                "FROM ev WHERE kind='context_assembly'"
            ).fetchone()
            or (None,)
        )[0]
        self.assertEqual(v, 12000)
        # the canonical 8-column schema, no filename leak
        cols = [d[0] for d in con.execute("SELECT * FROM ev LIMIT 0").description]
        self.assertEqual(
            cols, ["id", "session_id", "ts", "daemon", "session", "kind", "payload", "fleet"]
        )

    def test_default_connect_is_cached(self):
        con = engine.connect()
        self.assertIs(engine.connect(), con)


if __name__ == "__main__":
    unittest.main()
