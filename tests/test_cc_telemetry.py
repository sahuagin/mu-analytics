"""WS2 (mu-cc-event-unification-lkma.2): the cc full-fidelity emitter converts a
cc transcript into the whole mu-core SessionEvent stream with no information
loss. Contract: specs/architecture/cc-event-mapping.md (mu repo)."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
                "type": "message",
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
                "type": "message",
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

    def test_native_projection_is_consumed_not_just_validated(self):
        projected = {
            "id": "projected",
            "model": "projected-model",
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": "call", "name": "Read", "input": {"n": 2**64 - 1}}
            ],
            "usage": {"input_tokens": 23, "future_counter": 7},
        }
        binding = SimpleNamespace(response_message_to_py=Mock(return_value=projected))
        with patch.object(cct, "_MA", binding):
            norm = cct._normalize_assistant({"id": "original"})
        self.assertEqual(norm["model"], "projected-model")
        self.assertEqual(
            norm["tool_uses"],
            [{"id": "call", "name": "Read", "input": {"n": 2**64 - 1}}],
        )
        self.assertEqual(norm["raw_usage"], projected["usage"])
        self.assertEqual(cct._PARSE, {"typed": 1, "fallback": 0})
        binding.response_message_to_py.assert_called_once()

    def test_rejected_message_falls_back_locally_then_typed_parsing_resumes(self):
        good = _session_records()[1]["message"]
        bad = _session_records()[3]["message"]
        binding = SimpleNamespace(
            response_message_to_py=Mock(side_effect=[good, ValueError("bad field"), good])
        )
        errors = io.StringIO()
        with patch.object(cct, "_MA", binding), redirect_stderr(errors):
            first = cct._normalize_assistant(good, source="fixture.jsonl", line_number=1)
            fallback = cct._normalize_assistant(bad, source="fixture.jsonl", line_number=2)
            last = cct._normalize_assistant(good, source="fixture.jsonl", line_number=3)
        self.assertEqual(first, last)
        self.assertEqual(fallback["raw_usage"], bad["usage"])
        self.assertEqual(cct._PARSE, {"typed": 2, "fallback": 1})
        self.assertIn("fixture.jsonl:2: bad field", errors.getvalue())
        self.assertEqual(binding.response_message_to_py.call_count, 3)

    def test_unexpected_binding_error_is_not_hidden_as_protocol_fallback(self):
        binding = SimpleNamespace(
            response_message_to_py=Mock(side_effect=RuntimeError("binding defect"))
        )
        with patch.object(cct, "_MA", binding):
            with self.assertRaisesRegex(RuntimeError, "binding defect"):
                cct._normalize_assistant({})
        self.assertEqual(cct._PARSE["fallback"], 0)

    def test_wrapper_footer_and_invalid_json_are_distinguished(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "experiment.jsonl")
            with open(path, "w") as f:
                f.write("\n[loop-guard] saw result event; exiting cleanly\nnot json\n")
                for record in _session_records():
                    f.write(json.dumps(record) + "\n")
            errors = io.StringIO()
            with patch.object(cct, "_MA", None), redirect_stderr(errors):
                result = cct.convert_session(path)
            self.assertIsNotNone(result)
            self.assertEqual(cct._SKIPPED_TYPES["experiment_loop_guard_footer"], 1)
            self.assertEqual(cct._SKIPPED_TYPES["invalid_json_record"], 1)
            self.assertIn(f"{path}:3:", errors.getvalue())

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

    def test_injected_user_turns_tagged_meta(self):
        # skill bodies (isMeta) + tagged command/notification text are still emitted
        # (no information loss) but marked meta=true; genuine operator text is not —
        # so operator-language analytics can filter harness noise.
        records = [
            {
                "type": "user",
                "timestamp": "2026-06-15T10:00:00.000Z",
                "message": {"role": "user", "content": "please fix the bug"},
            },
            {  # skill body injected as an isMeta array-form turn
                "type": "user",
                "isMeta": True,
                "timestamp": "2026-06-15T10:00:01.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Base directory for this skill: /x"}],
                },
            },
            {  # slash command (string content, structural tag)
                "type": "user",
                "timestamp": "2026-06-15T10:00:02.000Z",
                "message": {"role": "user", "content": "<command-name>/goal</command-name> stop"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-06-15T10:00:03.000Z",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "model": "x",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        ]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sess-meta.jsonl")
            with open(p, "w") as f:
                for o in records:
                    f.write(json.dumps(o) + "\n")
            _sid, events = cct.convert_session(p)
        ums = self._by(events, "user_message")
        self.assertEqual(len(ums), 3)  # all emitted — no information loss
        operator = [u for u in ums if not u.get("meta")]
        injected = [u for u in ums if u.get("meta")]
        self.assertEqual([u["content"] for u in operator], ["please fix the bug"])
        self.assertEqual(len(injected), 2)  # skill body + slash-command turn


if __name__ == "__main__":
    unittest.main()
