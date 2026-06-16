"""DS1: session_timings ported onto the ev view. Unit-tests the four state-machine
paths (mu/cc × invocation/turn) directly on synthetic event rows, plus one engine
integration check that the SQL → state-machine wiring produces the right buckets."""

import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402
import session_timings as st  # noqa: E402

_T0 = int(datetime(2026, 6, 15, 13, 0, 0, tzinfo=UTC).timestamp() * 1000)
MU_META = ("gpt-5.5", "openai_codex")
CC_META = ("claude-opus-4-7", "claude_code")


def _row(kind, dt_ms, state=None, elapsed=0, out=0):
    """(kind, ts_ms, state, elapsed_ms, out_tok) — the shape _iter_session consumes."""
    return (kind, _T0 + dt_ms, state, elapsed, out)


def _calls(rows, fleet, meta, grouping):
    sess = "d1:session-1" if fleet == "mu" else "uuid-1"
    return list(st._iter_session(rows, fleet, sess, meta, grouping, None, None))


class TestStateMachines(unittest.TestCase):
    def test_mu_invocation(self):
        rows = [
            _row("provider_status_update", 0, state="awaiting_first_token"),
            _row("provider_status_update", 1000, state="streaming", elapsed=1000),
            _row("assistant_message_event", 3000, out=100),
            _row("provider_status_update", 4000, state="awaiting_first_token"),
            _row("provider_status_update", 4500, state="streaming", elapsed=500),
            _row("assistant_message_event", 6000, out=50),
        ]
        calls = _calls(rows, "mu", MU_META, "invocation")
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            (calls[0].duration_ms, calls[0].ttft_ms, calls[0].output_tokens), (3000.0, 1000.0, 100)
        )
        self.assertEqual(
            (calls[1].duration_ms, calls[1].ttft_ms, calls[1].output_tokens), (2000.0, 500.0, 50)
        )
        self.assertEqual(calls[0].model_norm, "gpt-5.5")
        self.assertEqual(calls[0].surface, "mu")

    def test_mu_turn(self):
        rows = [
            _row("user_message", 0),
            _row("provider_status_update", 500, state="awaiting_first_token"),
            _row("provider_status_update", 1500, state="streaming", elapsed=1000),
            _row("assistant_message_event", 3000, out=100),
            _row("provider_status_update", 3500, state="awaiting_first_token"),
            _row("provider_status_update", 4000, state="streaming", elapsed=500),
            _row("assistant_message_event", 5000, out=50),
            _row("done", 6000),
        ]
        calls = _calls(rows, "mu", MU_META, "turn")
        self.assertEqual(len(calls), 1)
        c = calls[0]
        # whole turn user->done; ttft = FIRST invocation's; invocations + tokens summed
        self.assertEqual(
            (c.duration_ms, c.ttft_ms, c.invocation_count, c.output_tokens),
            (6000.0, 1000.0, 2, 150),
        )

    def test_cc_invocation(self):
        # each assistant's delta from the preceding user_message OR tool_result
        rows = [
            _row("user_message", 0),
            _row("assistant_message_event", 2000, out=80),
            _row("tool_result", 2500),
            _row("assistant_message_event", 4000, out=40),
        ]
        calls = _calls(rows, "cc", CC_META, "invocation")
        self.assertEqual(len(calls), 2)
        self.assertEqual((calls[0].duration_ms, calls[0].output_tokens), (2000.0, 80))
        self.assertEqual((calls[1].duration_ms, calls[1].output_tokens), (1500.0, 40))
        self.assertIsNone(calls[0].ttft_ms)  # cc has no TTFT
        self.assertEqual(calls[0].surface, "claude-code")

    def test_cc_turn(self):
        rows = [
            _row("user_message", 0),
            _row("assistant_message_event", 2000, out=80),
            _row("tool_result", 2500),
            _row("assistant_message_event", 4000, out=40),
            _row("user_message", 10000),
            _row("assistant_message_event", 11000, out=20),
        ]
        calls = _calls(rows, "cc", CC_META, "turn")
        self.assertEqual(len(calls), 2)
        # turn 1: user -> last assistant, 2 invocations, tokens summed
        self.assertEqual(
            (calls[0].duration_ms, calls[0].invocation_count, calls[0].output_tokens),
            (4000.0, 2, 120),
        )
        self.assertEqual(
            (calls[1].duration_ms, calls[1].invocation_count, calls[1].output_tokens),
            (1000.0, 1, 20),
        )


def _ev(i, kind, payload):
    return {
        "id": i,
        "session_id": "session-1",
        "timestamp_unix_ms": _T0 + i * 1000,
        "actor": {},
        "payload": {"kind": kind, **payload},
    }


class TestComputeIntegration(unittest.TestCase):
    def test_mu_wiring_via_ev(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = os.path.join(tmp, "events", "d1", "session-1.jsonl")
            os.makedirs(os.path.dirname(ev))
            events = [
                _ev(1, "user_message", {"content": "go"}),
                _ev(
                    2, "provider_status_update", {"state": "awaiting_first_token", "elapsed_ms": 0}
                ),
                _ev(3, "provider_status_update", {"state": "streaming", "elapsed_ms": 1200}),
                _ev(4, "assistant_message_event", {"message": {"usage": {"output_tokens": 200}}}),
                _ev(5, "done", {"stop_reason": "end_turn", "elapsed_ms": 4000}),
                _ev(
                    6,
                    "task_telemetry",
                    {
                        "model": "gpt-5.5",
                        "provider_kind": "openai_codex",
                        "started_at_unix_ms": _T0,
                        "prompt_tokens": 100,
                        "completion_tokens": 200,
                    },
                ),
            ]
            with open(ev, "w") as f:
                for e in events:
                    f.write(json.dumps(e) + "\n")
            con = engine.connect(
                sources=[
                    (
                        os.path.join(tmp, "events", "*", "*.jsonl"),
                        "mu",
                        engine._MU_DAEMON,
                        engine._MU_SESSION,
                    )
                ]
            )
            report = st.compute(con, grouping="invocation")
        self.assertEqual(report.sessions_scanned, 1)
        s = report.by_surface_model[("mu", "gpt-5.5")]
        self.assertEqual(s.count, 1)
        self.assertEqual(s.ttft_ms, [1200.0])
        self.assertEqual(s.duration_ms, [2000.0])  # awaiting(id2,ts+2s) -> assistant(id4,ts+4s)


if __name__ == "__main__":
    unittest.main()
