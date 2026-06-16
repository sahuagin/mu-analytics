"""DS1: degradation_ml ported onto the unified substrate (degradation.assemble).
Hermetic — synthesizes two sessions (one scan-qualifying with a frustration hit,
one one-shot) and asserts the feature↔label join: X/y shape, the rows_j vs
unattended split, and the canonical session_ref keying. The sklearn training is
exercised by the live smoke, not here (KFold needs a real cohort)."""

import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import degradation  # noqa: E402
import engine  # noqa: E402
import features  # noqa: E402

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


class TestDegradationAssemble(unittest.TestCase):
    def test_join_and_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = os.path.join(tmp, "events")
            # A: 2 user msgs, mixed sentiment -> scan-qualifying (signed label).
            # msg1: 1 neg marker ("why are you"); msg2: 2 pos markers ("perfect", "thank").
            _write(
                os.path.join(ev, "d1", "session-1.jsonl"),
                [
                    _ev(1, "user_message", {"content": "why are you doing this"}),
                    _ev(2, "user_message", {"content": "that's perfect, thank you"}),
                    _ev(3, "tool_call", {"name": "Read", "call_id": "c1"}),
                    _ev(4, "done", {"stop_reason": "end_turn", "elapsed_ms": 2000}),
                    _ev(5, "task_telemetry", _tt()),
                ],
            )
            # B: 1 user msg -> NOT scan-qualifying -> unattended (telemetry only)
            _write(
                os.path.join(ev, "d2", "session-1.jsonl"),
                [
                    _ev(1, "user_message", {"content": "hi"}),
                    _ev(2, "task_telemetry", _tt()),
                ],
            )
            con = engine.connect(
                sources=[
                    (os.path.join(ev, "*", "*.jsonl"), "mu", engine._MU_DAEMON, engine._MU_SESSION)
                ]
            )
            a = degradation.assemble(con)

        self.assertEqual({r["session_ref"] for r in a["rows_j"]}, {"mu:d1:session-1"})
        self.assertEqual({r["session_ref"] for r in a["unattended"]}, {"mu:d2:session-1"})
        # X/X_un are plain lists (assemble is numpy-free → this runs in the lean CI
        # gate without the `ml` extra); each row aligns to the feature-name layout.
        self.assertEqual(len(a["X"]), 1)
        self.assertEqual(len(a["X"][0]), len(a["names"]))
        self.assertEqual(len(a["X_un"]), 1)
        self.assertEqual(len(a["X_un"][0]), len(a["names"]))
        self.assertIn("harness=mu", a["names"])
        self.assertIn("calls", a["names"])
        # signed y = 100 * net / n_user; net = pos(2) - neg(1) = +1, n_user=2 -> +50
        self.assertEqual(a["y"], [50.0])


if __name__ == "__main__":
    unittest.main()
