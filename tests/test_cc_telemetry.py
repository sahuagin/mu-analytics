"""WS2 (mu-cc-event-unification-lkma.2): the cc full-fidelity emitter converts a
cc transcript into the whole mu-core SessionEvent stream with no information
loss. Contract: specs/architecture/cc-event-mapping.md (mu repo)."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cc_telemetry as cct  # noqa: E402


def _session_records():
    """A representative cc transcript: user prompt -> assistant (thinking+text+
    tool_use) -> tool_result -> assistant (end_turn), plus a cc UI-metadata
    record that must be skipped, not crash."""
    return [
        {
            "type": "user",
            "timestamp": "2026-06-15T10:00:00.000Z",
            "message": {"role": "user", "content": "please fix it"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-06-15T10:00:01.000Z",
            "message": {
                "id": "msg_1",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "thinking", "thinking": "let me read first", "signature": "SIG=="},
                    {"type": "text", "text": "I'll read it."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "/x"},
                    },
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 8,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 5,
                        "ephemeral_1h_input_tokens": 3,
                    },
                },
            },
        },
        {
            "type": "user",
            "timestamp": "2026-06-15T10:00:02.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "file contents",
                        "is_error": False,
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-06-15T10:00:03.000Z",
            "message": {
                "id": "msg_2",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Done, fixed."}],
                "usage": {"input_tokens": 200, "output_tokens": 20},
            },
        },
        {"type": "file-history-snapshot", "timestamp": "2026-06-15T10:00:04.000Z"},
    ]


class TestCcTelemetry(unittest.TestCase):
    def setUp(self):
        # module-level visibility counters accumulate; reset for isolation.
        cct._UNMAPPED_BLOCKS.clear()
        cct._SKIPPED_TYPES.clear()
        cct._PARSE["typed"] = cct._PARSE["fallback"] = 0

    def _convert(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sess-abc.jsonl")
            with open(p, "w") as f:
                for o in _session_records():
                    f.write(json.dumps(o) + "\n")
            return cct.convert_session(p)

    def _by(self, events, kind):
        return [e["payload"] for e in events if e["payload"]["kind"] == kind]

    def test_full_stream_shape(self):
        sid, events = self._convert()
        self.assertEqual(sid, "sess-abc")
        kinds = [e["payload"]["kind"] for e in events]
        # Opens with SessionCreated, closes with the session-summed TaskTelemetry.
        self.assertEqual(kinds[0], "session_created")
        self.assertEqual(kinds[-1], "task_telemetry")
        for k in ("user_message", "assistant_message_event", "tool_call", "tool_result", "done"):
            self.assertIn(k, kinds, f"missing kind {k}")
        # ids are monotonic from 1 (the projector + event-log invariant).
        self.assertEqual([e["id"] for e in events], list(range(1, len(events) + 1)))

    def test_no_information_loss(self):
        sid, events = self._convert()
        # user text preserved
        self.assertEqual(len(self._by(events, "user_message")), 1)
        # tool_use -> ToolCall with REAL call_id + arguments (not stubs)
        tc = self._by(events, "tool_call")
        self.assertEqual(len(tc), 1)
        self.assertEqual(tc[0]["name"], "Read")
        self.assertEqual(tc[0]["call_id"], "toolu_1")
        self.assertEqual(tc[0]["arguments"], {"file_path": "/x"})
        # tool_result preserves call_id + is_error + content
        tr = self._by(events, "tool_result")[0]
        self.assertEqual(tr["call_id"], "toolu_1")
        self.assertFalse(tr["is_error"])
        self.assertEqual(tr["content"], "file contents")
        # assistant blocks: thinking(text-only, signature dropped) + text + tool_call
        blocks = self._by(events, "assistant_message_event")[0]["message"]["content"]
        self.assertEqual([b["type"] for b in blocks], ["thinking", "text", "tool_call"])
        self.assertNotIn("signature", blocks[0])
        # usage tier split (5m/1h) preserved on the turn
        u = self._by(events, "assistant_message_event")[0]["message"]["usage"]
        self.assertEqual(u["cache_creation_5m_input_tokens"], 5)
        self.assertEqual(u["cache_creation_1h_input_tokens"], 3)
        # Done on the terminal turn carries stop_reason + elapsed + usage
        dn = self._by(events, "done")[-1]
        self.assertEqual(dn["stop_reason"], "end_turn")
        self.assertIn("elapsed_ms", dn)
        # TaskTelemetry token sums == sum across turns -> sink cost parity
        tt = self._by(events, "task_telemetry")[0]
        self.assertEqual(tt["prompt_tokens"], 300)  # 100 + 200
        self.assertEqual(tt["completion_tokens"], 30)  # 10 + 20
        self.assertEqual(tt["cache_read_tokens"], 50)
        self.assertEqual(tt["cache_write_5m_tokens"], 5)
        self.assertEqual(tt["cache_write_1h_tokens"], 3)
        # nothing silently dropped on representative input
        self.assertEqual(sum(cct._UNMAPPED_BLOCKS.values()), 0)
        # the cc UI-metadata record was skipped (counted), not crashed/emitted
        self.assertEqual(cct._SKIPPED_TYPES.get("file-history-snapshot"), 1)

    def test_stop_sequence_normalizes_to_end_turn(self):
        # mu-consistent normalization (anthropic.rs:678): no StopSequence variant.
        self.assertEqual(cct._normalize_stop("stop_sequence"), "end_turn")
        self.assertEqual(cct._normalize_stop("tool_use"), "tool_use")
        self.assertEqual(cct._normalize_stop(None), "end_turn")


if __name__ == "__main__":
    unittest.main()
