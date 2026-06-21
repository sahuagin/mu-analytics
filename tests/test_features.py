"""DS1: per-session feature table over the ev view (features.session_features).
Hermetic — synthesizes task_telemetry / done / tool_call events and asserts the
token/tool/latency aggregation, config-driven cost, canonical session_ref, and
that an unlisted model prices to 0 (flagged, never guessed)."""

import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402
import features  # noqa: E402

# 2026-06-15 13:00:00 UTC is a Monday → hour=13, weekday=0.
_T0 = int(datetime(2026, 6, 15, 13, 0, 0, tzinfo=UTC).timestamp() * 1000)
PRICED_MODEL = next(iter(features.RATES))  # a model that exists in the rate table


def _ev(i, kind, payload, ts=None):
    return {
        "id": i,
        "session_id": "session-1",
        "timestamp_unix_ms": _T0 + (ts if ts is not None else i * 1000),
        "actor": {},
        "payload": {"kind": kind, **payload},
    }


def _write(path, events):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _connect(tmp, model=PRICED_MODEL):
    _write(
        os.path.join(tmp, "events", "d1", "session-1.jsonl"),
        [
            _ev(1, "user_message", {"content": "go"}),
            _ev(2, "tool_call", {"name": "Read", "call_id": "c1"}),
            _ev(3, "tool_call", {"name": "Bash", "call_id": "c2"}),
            _ev(4, "tool_call", {"name": "Grep", "call_id": "c3"}),
            _ev(5, "done", {"stop_reason": "end_turn", "elapsed_ms": 2000}),
            _ev(6, "done", {"stop_reason": "end_turn", "elapsed_ms": 4000}),
            _ev(
                7,
                "task_telemetry",
                {
                    "model": model,
                    "provider_kind": "anthropic",
                    "started_at_unix_ms": _T0,
                    "prompt_tokens": 1000,
                    "completion_tokens": 200,
                    "cache_read_tokens": 500,
                    "cache_write_tokens": 100,
                },
            ),
        ],
    )
    return engine.connect(
        sources=[
            (
                os.path.join(tmp, "events", "*", "*.jsonl"),
                "mu",
                engine._MU_DAEMON,
                engine._MU_SESSION,
            )
        ]
    )


class TestSessionFeatures(unittest.TestCase):
    def test_aggregation_and_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = features.session_features(_connect(tmp))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        # canonical key joins to the scans' output
        self.assertEqual(r["session_ref"], "mu:d1:session-1")
        # token backbone from task_telemetry
        self.assertEqual(r["input_tok"], 1000)
        self.assertEqual(r["output_tok"], 200)
        self.assertEqual(r["cache_read_tok"], 500)
        self.assertEqual(r["cache_write_tok"], 100)
        # tool_calls from tool_call events; calls + latency from done
        self.assertEqual(r["tool_calls"], 3)
        self.assertEqual(r["calls"], 2)
        self.assertEqual(r["wall_p50"], 3000.0)  # median of {2000, 4000}
        # time-of-week from started_at
        self.assertEqual(r["hour_of_day"], 13)
        self.assertEqual(r["day_of_week"], 0)
        # cost matches the config rate formula exactly
        self.assertEqual(r["cost_usd"], features.price(PRICED_MODEL, 1000, 200, 500, 100))
        self.assertGreater(r["cost_usd"], 0)

    def test_dashboard_noise_model_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = features.session_features(_connect(tmp, model="faux"))
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
