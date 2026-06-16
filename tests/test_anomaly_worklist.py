"""DS1: anomaly_worklist ported onto the unified substrate.
Hermetic — synthesizes a marked session (two operator_mark events in its OWN
stream, latest rating wins) and an unmarked one, then asserts:
  - build_table joins features.session_features to the session-keyed marks and
    partitions marked vs unmarked (the worklist-exclusion logic);
  - marks_store.read_marks_by_session keys by the canonical session_ref and takes
    the latest mark per session.
build_table is numpy-free, so this runs in the lean CI gate without the `ml`
extra; the IsolationForest scoring is exercised by the live smoke, not here."""

import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anomaly_worklist  # noqa: E402
import engine  # noqa: E402
import features  # noqa: E402
import marks_store  # noqa: E402

_T0 = int(datetime(2026, 6, 15, 13, 0, 0, tzinfo=UTC).timestamp() * 1000)
MODEL = next(iter(features.RATES))


def _ev(i, kind, payload):
    return {
        "id": i,
        "session_id": "session-1",
        "timestamp_unix_ms": _T0 + i * 1000,
        "actor": {},
        "payload": {"kind": kind, **payload},
    }


def _write(path, events):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _tt():
    return {
        "model": MODEL,
        "provider_kind": "anthropic",
        "started_at_unix_ms": _T0,
        "prompt_tokens": 1000,
        "completion_tokens": 200,
    }


def _connect(tmp):
    ev = os.path.join(tmp, "events")
    # A: priced + marked twice (rating 2 then 5) -> latest (5) wins, excluded from worklist
    _write(
        os.path.join(ev, "d1", "session-1.jsonl"),
        [
            _ev(1, "user_message", {"content": "go"}),
            _ev(2, "task_telemetry", _tt()),
            _ev(3, "operator_mark", {"rating": 2, "note": "first pass"}),
            _ev(4, "operator_mark", {"rating": 5, "note": "revised up"}),
        ],
    )
    # B: priced + unmarked -> belongs in the worklist
    _write(
        os.path.join(ev, "d2", "session-1.jsonl"),
        [
            _ev(1, "user_message", {"content": "hi"}),
            _ev(2, "task_telemetry", _tt()),
        ],
    )
    return engine.connect(
        sources=[(os.path.join(ev, "*", "*.jsonl"), "mu", engine._MU_DAEMON, engine._MU_SESSION)]
    )


class TestAnomalyWorklist(unittest.TestCase):
    def test_marks_by_session_latest_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            marks = marks_store.read_marks_by_session(_connect(tmp))
        # canonical session_ref keying; only the marked session present
        self.assertEqual(set(marks), {"mu:d1:session-1"})
        # latest operator_mark (rating 5) wins over the earlier (rating 2)
        self.assertEqual(marks["mu:d1:session-1"]["rating"], 5)
        self.assertEqual(marks["mu:d1:session-1"]["source"], "mu_event")

    def test_build_table_join_and_partition(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = anomaly_worklist.build_table(_connect(tmp))
        refs = {r["session_ref"] for r in t["feat"]}
        self.assertEqual(refs, {"mu:d1:session-1", "mu:d2:session-1"})
        # the marked session is annotated and partitioned out of the worklist
        self.assertEqual({r["session_ref"] for r in t["marked"]}, {"mu:d1:session-1"})
        self.assertEqual({r["session_ref"] for r in t["unmarked"]}, {"mu:d2:session-1"})
        a = next(r for r in t["feat"] if r["session_ref"] == "mu:d1:session-1")
        b = next(r for r in t["feat"] if r["session_ref"] == "mu:d2:session-1")
        self.assertEqual(a["rating"], 5)
        self.assertIsNone(b["rating"])


if __name__ == "__main__":
    unittest.main()
