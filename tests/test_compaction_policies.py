"""compaction_policies: the counterfactual hook and the two non-logged policies.

Hermetic: ropes are built directly on a ReplayState. Asserts the span-family-drop
port follows heuristic.rs tier order (oldest tool cluster + its assistant first,
then old assistants, recent assistants preserved, call_id reconciliation closes
units), that the lexical policy evicts the LEAST intent-relevant unit first and
never evicts users, and that the replay applies a policy's decisions in place of
the logged ones and counts the recall-miss proxy.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache_sim as cs  # noqa: E402
import compaction_policies as cp  # noqa: E402


def _state_with_exchange(n_rounds: int, result_chars: int = 400) -> cs.ReplayState:
    """user, then n_rounds of (assistant tool_call -> tool_result), then a final assistant."""
    st = cs.ReplayState()
    st.add("user", 40, "hu", content="please read the config file and fix the parser bug")
    for i in range(n_rounds):
        cid = f"c{i}"
        st.add(
            "assistant",
            60,
            f"ha{i}",
            content=f"read file{i}",
            tool_calls=((cid, "read", f"args{i}"),),
        )
        st.add("tool_result", result_chars, f"hr{i}", call_id=cid, content=f"contents of file{i}")
    st.add("assistant", 30, "hfinal", content="done")
    return st


def _payload(threshold: int, tokens_before: int) -> dict:
    return {
        "kind": "compaction_assembly",
        "model_call_id": 7,
        "compaction_threshold": threshold,
        "tokens_before": tokens_before,
        "tokens_after": 0,
        "decisions": [],
    }


class TestSpanFamilyDropPort(unittest.TestCase):
    def test_oldest_cluster_and_its_assistant_go_first(self):
        st = _state_with_exchange(4)
        # 4 rounds * (60+400)/4 + user 10 + final ~8 = ~478 raw tokens; target 300
        p = _payload(threshold=600, tokens_before=478)
        dec = cp.SpanFamilyDropPort().compact(st, p)
        ids = [d["span_id"] for d in dec]
        # oldest exchange (idx 1 assistant, idx 2 result) must be dropped first
        self.assertIn("msg-1-assistant", ids)
        self.assertIn("msg-2-tool-result:c0", ids)
        # the two most recent assistants (idx 7 and final idx 9) are preserved
        self.assertNotIn("msg-7-assistant", ids)
        self.assertNotIn("msg-9-assistant", ids)
        # the user is never dropped
        self.assertNotIn("msg-0-user", ids)
        # a result is never dropped without its producing assistant (unit integrity)
        for d in dec:
            if "tool-result" in d["span_id"]:
                cid = d["span_id"].split(":")[-1]
                prod = next(m for m in st.msgs if any(tc[0] == cid for tc in m.tool_calls))
                self.assertIn(prod.span_id, ids)

    def test_no_drops_when_under_target(self):
        st = _state_with_exchange(2)
        p = _payload(threshold=100_000, tokens_before=200)
        self.assertEqual(cp.SpanFamilyDropPort().compact(st, p), [])

    def test_preserved_recent_unit_is_kept_whole(self):
        # Tiny target forces everything droppable out; the preserved assistants'
        # results must come back via reconciliation (kept whole).
        st = _state_with_exchange(3)
        p = _payload(threshold=2, tokens_before=400)
        dec = cp.SpanFamilyDropPort().compact(st, p)
        ids = {d["span_id"] for d in dec}
        self.assertNotIn("msg-5-assistant", ids)  # 2nd most recent assistant
        self.assertNotIn("msg-6-tool-result:c2", ids)  # its result rides along
        self.assertIn("msg-1-assistant", ids)
        self.assertIn("msg-3-assistant", ids)


class TestLexicalPolicy(unittest.TestCase):
    def test_least_relevant_unit_goes_first(self):
        st = cs.ReplayState()
        st.add("user", 40, "hu", content="fix the parser bug in tokenizer.rs")
        st.add("assistant", 60, "ha0", content="read weather", tool_calls=(("c0", "read", "a0"),))
        st.add("tool_result", 400, "hr0", call_id="c0", content="sunny forecast rain clouds")
        st.add("assistant", 60, "ha1", content="read tokenizer", tool_calls=(("c1", "read", "a1"),))
        st.add(
            "tool_result", 400, "hr1", call_id="c1", content="tokenizer.rs parser bug on line 40"
        )
        st.add("assistant", 60, "ha2", content="look", tool_calls=(("c2", "read", "a2"),))
        st.add("tool_result", 400, "hr2", call_id="c2", content="unrelated grocery list")
        st.add("assistant", 30, "hfin", content="working")
        st.add("assistant", 30, "hfin2", content="still working")
        # drop exactly one unit: target just under the total
        total = sum(m.chars for m in st.msgs) / 4.0
        p = _payload(threshold=int((total - 50) * 2), tokens_before=int(total))
        dec = cp.LexicalRelevancePolicy().compact(st, p)
        ids = {d["span_id"] for d in dec}
        self.assertNotIn("msg-4-tool-result:c1", ids, "the intent-relevant unit must survive")
        self.assertNotIn("msg-0-user", ids)
        self.assertTrue(ids & {"msg-2-tool-result:c0", "msg-6-tool-result:c2"})

    def test_units_pair_results_to_producers_by_call_id(self):
        st = _state_with_exchange(2)
        units = cp.exchange_units(st.live())
        # user excluded; 2 exchange units + final lone assistant
        self.assertEqual(len(units), 3)
        self.assertIn([1, 2], units)
        self.assertIn([3, 4], units)


class TestReplayHook(unittest.TestCase):
    def _write(self, events):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "session-1.jsonl")
        with open(p, "w") as fh:
            for i, (kind, payload) in enumerate(events, 1):
                fh.write(
                    json.dumps(
                        {
                            "id": i,
                            "session_id": "session-1",
                            "timestamp_unix_ms": i,
                            "actor": {"kind": "system"},
                            "payload": {"kind": kind, **payload},
                        }
                    )
                    + "\n"
                )
        return p

    def test_policy_replaces_logged_decisions_and_dups_are_counted(self):
        class DropNothing:
            name = "noop"

            def compact(self, st, p):
                return []

        asm = {
            "model_call_id": 1,
            "message_count": 4,
            "tool_count": 1,
            "span_count": 7,
            "token_breakdown": {
                "system": 10,
                "tool_schema": 5,
                "user": 10,
                "assistant": 10,
                "tool_result": 100,
            },
            "provider_kind": "vllm",
            "model": "m",
            "first_span_ids": ["project-file:x"],
        }
        events = [
            ("session_created", {"provider_kind": "vllm", "model": "m", "usage_semantics": {}}),
            ("user_message", {"content": "go"}),  # 0
            (
                "assistant_message_event",
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_call",
                                "id": "c1",
                                "name": "read",
                                "arguments": {"p": "a"},
                            }
                        ]
                    }
                },
            ),  # 1
            ("tool_call", {"call_id": "c1", "name": "read", "arguments": {"p": "a"}}),
            ("tool_result", {"call_id": "c1", "content": "x" * 400, "is_error": False}),  # 2
            (
                "assistant_message_event",
                {"message": {"content": [{"type": "text", "text": "ok"}]}},
            ),  # 3
            (
                "compaction_assembly",
                {
                    "model_call_id": 1,
                    "policy_id": "span-family-drop",
                    "tokens_before": 300,
                    "tokens_after": 100,
                    "compaction_threshold": 200,
                    "decisions": [
                        {"action": "dropped", "span_id": "msg-1-assistant", "reason": "r"},
                        {"action": "dropped", "span_id": "msg-2-tool-result:c1", "reason": "r"},
                    ],
                },
            ),
            ("context_assembly", asm),
            # the agent re-issues the identical call
            (
                "assistant_message_event",
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_call",
                                "id": "c2",
                                "name": "read",
                                "arguments": {"p": "a"},
                            }
                        ]
                    }
                },
            ),  # 4
            ("tool_call", {"call_id": "c2", "name": "read", "arguments": {"p": "a"}}),
            ("tool_result", {"call_id": "c2", "content": "x" * 400, "is_error": False}),  # 5
            ("context_assembly", {**asm, "model_call_id": 2, "message_count": 6, "span_count": 9}),
        ]
        path = self._write(events)
        # logged: the first result was dropped before the re-issue -> dup_after_drop
        rows, stats = cs.simulate_session_full(path)
        self.assertEqual(stats.compactions, 1)
        self.assertEqual((stats.dup_after_drop, stats.dup_while_live), (1, 0))
        self.assertEqual(rows[0].spans, 2 + 2)  # prefix pair + user + final assistant
        # counterfactual: keep everything -> the re-issue happens with the result live
        rows2, stats2 = cs.simulate_session_full(path, policy=DropNothing(), calibration="session")
        self.assertEqual((stats2.dup_after_drop, stats2.dup_while_live), (0, 1))
        self.assertEqual(rows2[0].spans, 2 + 4)
        self.assertEqual(stats2.decision_jaccard_sum, 0.0)


if __name__ == "__main__":
    unittest.main()
