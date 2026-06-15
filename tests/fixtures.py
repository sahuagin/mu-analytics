"""Synthetic event-log fixture for hermetic tests — no live data, no real sinks.

Writes one daemon/session JSONL with a representative event of each kind the
panels query, then hands back a DuckDB connection with the `ev` view registered
over it (via engine.connect's parameterized glob). Tests assert the query logic
against these known events.
"""

import json
import os
import sys

# tests/ is not a package and the modules live at the repo root; make them importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402

_T0 = 1_700_000_000_000  # fixed epoch-ms base (no wall-clock dependence)


def _events():
    def ev(i, kind, payload):
        return {
            "id": i,
            "session_id": "session-1",
            "timestamp_unix_ms": _T0 + i * 60_000,
            "actor": "agent",
            "payload": {"kind": kind, **payload},
        }

    return [
        ev(1, "session_created", {}),
        ev(2, "user_message", {"text": "hi"}),
        ev(
            3,
            "context_assembly",
            {
                "token_count_estimate": 12000,
                "token_breakdown": {"system": 100, "tool_schema": 200},
                "prefix_hash": "abc123",
            },
        ),
        ev(
            4,
            "assistant_message_event",
            {
                "message": {
                    "stop_reason": "tool_use",
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 5000,
                        "cache_creation_input_tokens": 2000,
                    },
                }
            },
        ),
        ev(5, "tool_call", {"name": "bash", "call_id": "c1"}),
        ev(6, "tool_result", {"call_id": "c1", "is_error": False}),
        ev(7, "tool_call", {"name": "read", "call_id": "c2"}),
        ev(8, "tool_result", {"call_id": "c2", "is_error": True}),
        ev(
            9,
            "recall_provenance",
            {
                "items": [
                    {"source": "memory", "token_count": 1234},
                    {"source": "project_file", "token_count": 5678},
                ]
            },
        ),
        ev(
            10,
            "compaction_assembly",
            {
                "tokens_before": 100000,
                "tokens_after": 20000,
                "decisions": [{"action": "dropped", "span_id": "s1", "reason": "old"}],
            },
        ),
        ev(11, "operator_mark", {"rating": "4", "note": "fixture mark"}),
        ev(12, "callout", {"title": "fixture flag", "category": "warning"}),
        ev(
            13,
            "done",
            {
                "stop_reason": "degraded_eof",
                "turn_count": 1,
                "usage": {"input_tokens": 1000, "output_tokens": 50},
            },
        ),
        ev(
            14,
            "task_telemetry",
            {
                "task_id": "task-fixture-1",
                "session_id": "session-1",
                "provider_kind": "anthropic_api",
                "model": "claude-opus-4-8",
                "ended_at_unix_ms": _T0 + 900_000,
                "wall_clock_ms": 900_000,
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "cache_read_tokens": 5000,
                "cache_write_tokens": 2000,
                "cache_write_5m_tokens": 500,
                "cache_write_1h_tokens": 1500,
                "exit_reason": "error",
            },
        ),
    ]


def write_event_log(tmpdir):
    """Write the fixture events under <tmpdir>/events/testdaemon01/; return the glob."""
    daemon = os.path.join(tmpdir, "events", "testdaemon01")
    os.makedirs(daemon, exist_ok=True)
    with open(os.path.join(daemon, "session-1.jsonl"), "w") as f:
        for e in _events():
            f.write(json.dumps(e) + "\n")
    return os.path.join(tmpdir, "events", "*", "*.jsonl")


def fixture_connection(tmpdir):
    """A DuckDB connection with `ev` registered over the fixture event log."""
    return engine.connect(glob=write_event_log(tmpdir), fleet="mu")


# --- cc fleet fixture (WS3) ---------------------------------------------------
# The cc emitter (WS2) writes one JSONL per session under a provider dir:
#   <cc_events_out>/claude-code/<session-uuid>.jsonl
# carrying the full SessionEvent stream — the rich behavioral kinds the MVP
# summary lacked. session_id is the unique per-session key for cc.

_CC_SESSION = "cc11111-2222-3333-4444-555555555555"


def _cc_events(session_id=_CC_SESSION):
    def ev(i, kind, payload):
        return {
            "id": i,
            "session_id": session_id,
            "timestamp_unix_ms": _T0 + i * 60_000,
            "actor": {"kind": "agent"},  # cc actor is an object, not a bare string
            "payload": {"kind": kind, **payload},
        }

    return [
        ev(1, "session_created", {"provider_kind": "claude_code", "model": "claude-x"}),
        ev(2, "user_message", {"text": "do the thing"}),
        ev(
            3,
            "assistant_message_event",
            {"message": {"stop_reason": "tool_use", "usage": {"input_tokens": 10}}},
        ),
        ev(4, "tool_call", {"name": "Read", "call_id": "cc1"}),
        ev(5, "tool_result", {"call_id": "cc1", "is_error": False}),
        ev(6, "done", {"stop_reason": "end_turn", "turn_count": 1, "usage": {}}),
        ev(7, "task_telemetry", {"task_id": "t-cc-1", "session_id": session_id}),
    ]


def write_cc_event_log(tmpdir, session_id=_CC_SESSION):
    """Write a rich cc session under <tmpdir>/cc-events/claude-code/<id>.jsonl; glob."""
    provider = os.path.join(tmpdir, "cc-events", "claude-code")
    os.makedirs(provider, exist_ok=True)
    with open(os.path.join(provider, f"{session_id}.jsonl"), "w") as f:
        for e in _cc_events(session_id):
            f.write(json.dumps(e) + "\n")
    return os.path.join(tmpdir, "cc-events", "*", "*.jsonl")


def both_fleets_connection(tmpdir):
    """A connection whose `ev` view unions a mu log and a cc log (WS3 shape)."""
    mu_glob = write_event_log(tmpdir)
    cc_glob = write_cc_event_log(tmpdir)
    return engine.connect(
        sources=[
            (mu_glob, "mu", engine._MU_DAEMON),
            (cc_glob, "cc", engine._CC_DAEMON),
        ]
    )
