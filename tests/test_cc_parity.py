"""WS4 (mu-cc-event-unification-lkma.4): round-trip / no-loss parity for the cc
emitter, per account shape.

WS2's test_cc_telemetry.py proves the full-stream shape on one anthropic-shaped
transcript. This adds a REUSABLE count/token parity harness that derives the
expected totals from the SOURCE records and asserts the emitted SessionEvent
stream matches — run against synthetic fixtures for each account shape
(claude_code = personal/work; openrouter = pay-per-token, provider-prefixed
model, detected by the .claude-openrouter path).

Fixtures are synthetic (no real transcript content) — mu-analytics is a public
repo; real cc transcripts carry conversation PII. The shapes mirror the live
accounts: claude-code normalizes the transcript format across providers, so the
usage schema is identical; the meaningful axes are the provider path (drives
provider_kind / cost_kind) and the model name (openrouter is provider-prefixed).
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cc_telemetry as cct  # noqa: E402

_T = "2026-06-15T10:00:0"  # + index + "Z"; one-second steps keep elapsed deterministic


def _asst(mid, model, stop_reason, blocks, usage, i):
    return {
        "type": "assistant",
        "timestamp": f"{_T}{i}.000Z",
        "message": {
            "id": mid,
            "role": "assistant",
            "model": model,
            "stop_reason": stop_reason,
            "content": blocks,
            "usage": usage,
        },
    }


def _user(text, i):
    return {
        "type": "user",
        "timestamp": f"{_T}{i}.000Z",
        "message": {"role": "user", "content": text},
    }


def _tool_result(call_id, content, is_error, i):
    return {
        "type": "user",
        "timestamp": f"{_T}{i}.000Z",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    }


def _usage(inp, out, cr=0, cw=0, e5=0, e1=0):
    u = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw,
    }
    if e5 or e1:
        u["cache_creation"] = {"ephemeral_5m_input_tokens": e5, "ephemeral_1h_input_tokens": e1}
    return u


def _claude_code_transcript():
    """Anthropic shape (personal/work): a tool_use turn then a terminal turn,
    plus a second ask. Two terminal turns → two Done events; one tool round-trip."""
    return [
        _user("please fix it", 0),
        _asst(
            "m1",
            "claude-opus-4-8",
            "tool_use",
            [
                {"type": "text", "text": "reading"},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x"}},
            ],
            _usage(100, 10, cr=50, cw=8, e5=5, e1=3),
            1,
        ),
        _tool_result("t1", "file contents", False, 2),
        _asst(
            "m2",
            "claude-opus-4-8",
            "end_turn",
            [{"type": "text", "text": "fixed"}],
            _usage(200, 20),
            3,
        ),
        _user("now test it", 4),
        _asst(
            "m3",
            "claude-opus-4-8",
            "end_turn",
            [{"type": "text", "text": "tests pass"}],
            _usage(300, 30, cr=10),
            5,
        ),
        {"type": "file-history-snapshot", "timestamp": f"{_T}6.000Z"},  # skipped, not crashed
    ]


def _openrouter_transcript():
    """Openrouter shape: provider-prefixed model; single ask, one tool round-trip."""
    return [
        _user("summarize", 0),
        _asst(
            "o1",
            "google/gemma-4-31b-it-20260402",
            "tool_use",
            [{"type": "tool_use", "id": "g1", "name": "Grep", "input": {"q": "foo"}}],
            _usage(80, 5),
            1,
        ),
        _tool_result("g1", "3 hits", False, 2),
        _asst(
            "o2",
            "google/gemma-4-31b-it-20260402",
            "end_turn",
            [{"type": "text", "text": "done"}],
            _usage(90, 12),
            3,
        ),
    ]


# ---- parity harness: expected totals derived from the SOURCE records ----------


def _source_stats(records):
    """Compute the no-loss-expected totals straight from the raw cc records,
    mirroring the emitter's dedup-by-id (last wins) and token summation."""
    user_text = tool_use = tool_result = 0
    tr_errors = []
    asst = {}  # mid -> (usage, stop_reason_normalized, is_terminal, n_tool_use)
    order = []
    for o in records:
        t = o.get("type")
        msg = o.get("message")
        if t == "assistant" and isinstance(msg, dict):
            mid = msg.get("id")
            blocks = msg.get("content") or []
            ntu = sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use")
            sr = cct._normalize_stop(msg.get("stop_reason"))
            if mid not in asst:
                order.append(mid)
            asst[mid] = (msg.get("usage") or {}, sr, sr in ("end_turn", "max_tokens"), ntu)
        elif t == "user" and isinstance(msg, dict):
            c = msg.get("content")
            if isinstance(c, str):
                user_text += 1
            elif isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        tool_result += 1
                        tr_errors.append(bool(b.get("is_error", False)))
                    elif b.get("type") == "text" and b.get("text"):
                        user_text += 1

    pt = ct = cr = cw = cw5 = cw1 = 0
    done_inp = terminals = 0
    stop_reasons = []
    for mid in order:
        u, sr, terminal, ntu = asst[mid]
        tool_use += ntu
        stop_reasons.append(sr)
        pt += u.get("input_tokens", 0) or 0
        ct += u.get("output_tokens", 0) or 0
        cr += u.get("cache_read_input_tokens", 0) or 0
        cw += u.get("cache_creation_input_tokens", 0) or 0
        cc = u.get("cache_creation")
        if isinstance(cc, dict):
            cw5 += cc.get("ephemeral_5m_input_tokens", 0) or 0
            cw1 += cc.get("ephemeral_1h_input_tokens", 0) or 0
        if terminal:
            terminals += 1
            done_inp += u.get("input_tokens", 0) or 0
    return {
        "user_text": user_text,
        "tool_use": tool_use,
        "tool_result": tool_result,
        "tr_errors": tr_errors,
        "pt": pt,
        "ct": ct,
        "cr": cr,
        "cw": cw,
        "cw5": cw5,
        "cw1": cw1,
        "done_inp": done_inp,
        "terminals": terminals,
        "stop_reasons": stop_reasons,
    }


def _emitted_stats(events):
    def by(kind):
        return [e["payload"] for e in events if e["payload"]["kind"] == kind]

    tt = by("task_telemetry")[0]
    dones = by("done")
    return {
        "user_text": len(by("user_message")),
        "tool_use": len(by("tool_call")),
        "tool_result": len(by("tool_result")),
        "tr_errors": [p["is_error"] for p in by("tool_result")],
        "pt": tt.get("prompt_tokens", 0),
        "ct": tt.get("completion_tokens", 0),
        "cr": tt.get("cache_read_tokens", 0),
        "cw": tt.get("cache_write_tokens", 0),
        "cw5": tt.get("cache_write_5m_tokens", 0),
        "cw1": tt.get("cache_write_1h_tokens", 0),
        "done_inp": sum((d.get("usage") or {}).get("input_tokens", 0) for d in dones),
        "terminals": len(dones),
        "stop_reasons": [p["message"]["stop_reason"] for p in by("assistant_message_event")],
        "provider_kind": by("session_created")[0]["provider_kind"],
    }


class TestCcParity(unittest.TestCase):
    def setUp(self):
        cct._UNMAPPED_BLOCKS.clear()
        cct._SKIPPED_TYPES.clear()
        cct._PARSE["typed"] = cct._PARSE["fallback"] = 0

    def _run(self, records, rel_path):
        """Write records at <tmp>/<rel_path> (the path drives provider detection),
        convert, and return (source_stats, emitted_stats)."""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, rel_path)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                for o in records:
                    f.write(json.dumps(o) + "\n")
            result = cct.convert_session(p)
            self.assertIsNotNone(result, "convert_session returned None")
            _sid, events = result
            return _source_stats(records), _emitted_stats(events)

    def _assert_parity(self, src, emit):
        # counts round-trip with zero loss
        self.assertEqual(emit["user_text"], src["user_text"], "UserMessage count")
        self.assertEqual(emit["tool_use"], src["tool_use"], "ToolCall count")
        self.assertEqual(emit["tool_result"], src["tool_result"], "ToolResult count")
        self.assertEqual(emit["tr_errors"], src["tr_errors"], "is_error flags preserved")
        # token sums == source (and therefore == the MVP sink sum: cost parity)
        for k in ("pt", "ct", "cr", "cw", "cw5", "cw1"):
            self.assertEqual(emit[k], src[k], f"token sum {k}")
        # stop_reasons preserved (normalized), one Done per terminal turn, and
        # Σ Done.usage is exactly the terminal-turn slice of the telemetry sum
        self.assertEqual(emit["stop_reasons"], src["stop_reasons"], "stop_reasons")
        self.assertEqual(emit["terminals"], src["terminals"], "one Done per terminal turn")
        self.assertEqual(emit["done_inp"], src["done_inp"], "Σ Done.usage == terminal turns")
        # nothing silently dropped
        self.assertEqual(sum(cct._UNMAPPED_BLOCKS.values()), 0, "no unmapped blocks")

    def test_parity_claude_code(self):
        src, emit = self._run(_claude_code_transcript(), "proj/85c964f7-claude-code.jsonl")
        self._assert_parity(src, emit)
        self.assertEqual(emit["provider_kind"], "claude_code")
        # the non-conversation record was skipped, not emitted/crashed
        self.assertEqual(cct._SKIPPED_TYPES.get("file-history-snapshot"), 1)

    def test_parity_openrouter(self):
        # path under .claude-openrouter → provider_kind = openrouter (pay-per-token)
        src, emit = self._run(
            _openrouter_transcript(), ".claude-openrouter/projects/p/d83eace4.jsonl"
        )
        self._assert_parity(src, emit)
        self.assertEqual(emit["provider_kind"], "openrouter")

    def test_stop_sequence_normalizes_end_to_end(self):
        # a raw 'stop_sequence' turn must surface as 'end_turn' in the stream
        records = [
            _user("hi", 0),
            _asst(
                "s1",
                "claude-opus-4-8",
                "stop_sequence",
                [{"type": "text", "text": "bye"}],
                _usage(10, 2),
                1,
            ),
        ]
        src, emit = self._run(records, "proj/s.jsonl")
        self.assertEqual(emit["stop_reasons"], ["end_turn"])
        self._assert_parity(src, emit)

    def test_unknown_user_block_is_counted_not_dropped(self):
        # an unmapped block type must be COUNTED (the failure this goal exists to
        # prevent: a silent drop). It is not part of the no-loss counts.
        records = [
            _user("go", 0),
            {
                "type": "user",
                "timestamp": f"{_T}1.000Z",
                "message": {"role": "user", "content": [{"type": "image", "source": {}}]},
            },  # unmapped
            _asst(
                "u1",
                "claude-opus-4-8",
                "end_turn",
                [{"type": "text", "text": "ok"}],
                _usage(10, 2),
                2,
            ),
        ]
        self._run(records, "proj/u.jsonl")
        self.assertEqual(cct._UNMAPPED_BLOCKS.get("user:image"), 1)


if __name__ == "__main__":
    unittest.main()
