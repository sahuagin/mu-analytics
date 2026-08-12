"""Hermetic tests for the lexical signature detectors (signatures.py).

Fixture: mu-format event JSONL with assistant_message_event text blocks, wired
through engine.connect(glob=...) exactly like test_scans — no live data.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402
import signatures  # noqa: E402
from scans import ET  # noqa: E402

LONG_A = (
    "the deploy pipeline rebuilds the container image then runs the smoke suite and "
    "finally promotes the image to the stable channel after the health checks pass "
    "which keeps the rollout gated on evidence instead of on optimism about the build"
)
LONG_B = (
    "database migrations run inside a transaction and roll back cleanly when any "
    "statement fails so partial schema changes never reach the production replica "
    "and the operator can retry the whole batch after fixing the offending statement"
)


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _write(path, session_id, turns):
    """turns: (when, text) assistant turns; text=None emits a tool-call-only event."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for i, (when, text) in enumerate(turns, 1):
            content = (
                [{"type": "text", "text": text}]
                if text is not None
                else [{"type": "tool_call", "name": "bash", "arguments": {}}]
            )
            f.write(
                json.dumps(
                    {
                        "id": i,
                        "session_id": session_id,
                        "timestamp_unix_ms": _ms(when),
                        "actor": "agent",
                        "payload": {
                            "kind": "assistant_message_event",
                            "message": {"content": content, "usage": {"input_tokens": 1000}},
                        },
                    }
                )
                + "\n"
            )


D1 = datetime(2026, 6, 15, 21, 0, tzinfo=ET)  # Monday evening ET
D2 = datetime(2026, 6, 16, 10, 0, tzinfo=ET)  # Tuesday morning ET


class SignaturesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ev = os.path.join(self.tmp.name, "events")
        # s1: a long turn, an unrelated long turn, then a near-verbatim repeat of the
        # first — the repeat must flag. Also a short ack + a tool-only event (ignored).
        _write(
            os.path.join(ev, "d1", "session-1.jsonl"),
            "session-1",
            [
                (D1, LONG_A),
                (D1, None),
                (D1, "ok."),
                (D1, LONG_B),
                (D2, LONG_A + " as noted before"),
            ],
        )
        # s2: closes D1 with the same boilerplate as s1's D1 closing (template pair),
        # closes D2 with unrelated prose (no pair).
        _write(
            os.path.join(ev, "d2", "session-1.jsonl"),
            "session-1",
            [(D1, LONG_B), (D2, "totally different words that share no five word runs " * 4)],
        )
        self.con = engine.connect(glob=os.path.join(ev, "*", "*.jsonl"))
        self.turns = signatures.assistant_turns(self.con)

    def tearDown(self):
        self.tmp.cleanup()

    def test_turn_extraction_skips_toolcalls_and_keeps_prose(self):
        (s1,) = [v for k, v in self.turns.items() if ":d1:" in k]
        # 4 prose turns survive (tool-only event dropped); short ack kept here —
        # word-count gating happens in the detectors, not extraction.
        self.assertEqual(len(s1), 4)

    def test_repetition_flags_near_verbatim_reemission(self):
        rec = signatures.scan_repetition(self.turns)
        s1 = [r for r in rec if ":d1:" in r["ref"]]
        # "ok." is below MIN_TURN_WORDS -> 3 records; only the D2 re-emission repeats.
        self.assertEqual(len(s1), 3)
        repeats = [r for r in s1 if r["repeat"]]
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]["et_date"], "2026-06-16")
        self.assertGreaterEqual(repeats[0]["best_sim"], 0.5)
        # every record carries its own ET hour + ctx
        self.assertTrue(all("et_hour" in r and "ctx" in r for r in s1))
        self.assertEqual(s1[0]["et_hour"], 21)

    def test_closing_pairs_cross_session_only(self):
        closings = signatures.day_closings(self.turns)
        # s1 closes twice (D1, D2), s2 closes twice -> 4 day-closings
        self.assertEqual(len(closings), 4)
        pairs = signatures.closing_pairs(closings)
        # exactly one cross-session template pair: s1@D1(LONG_B) ~ s2@D1(LONG_B)...
        # s1's D1 closing is LONG_B (last substantial turn of that day).
        self.assertEqual(len(pairs), 1)
        p = pairs[0]
        self.assertEqual({p["a_date"], p["b_date"]}, {"2026-06-15"})
        self.assertNotEqual(p["a_ref"], p["b_ref"])
        self.assertGreaterEqual(p["sim"], 0.9)


if __name__ == "__main__":
    unittest.main()
