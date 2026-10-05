"""cache_sim: replay + cache-model unit tests on a synthetic session log.

Hermetic: one hand-built JSONL with a continuation seed, live turns, a compaction
and a context clear. Asserts (a) the replay reproduces the snapshot counts the way
the probe did on a real session, (b) the cache models produce the hand-computed
prefill numbers for the interior-edit case, (c) the validation hook reads usage
under both cache_read_in_input semantics.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache_sim as cs  # noqa: E402

INF = cs.INF


def _ev(i, kind, **payload):
    return {
        "id": i,
        "session_id": "session-1",
        "timestamp_unix_ms": 1_700_000_000_000 + i,
        "actor": {"kind": "system"},
        "payload": {"kind": kind, **payload},
    }


def _assembly(i, call_id, msgs, span_count, breakdown, tool_count=2, first=None):
    return _ev(
        i,
        "context_assembly",
        model_call_id=call_id,
        message_count=msgs,
        user_message_count=0,
        assistant_message_count=0,
        tool_result_count=0,
        tool_count=tool_count,
        token_count_estimate=sum(breakdown.values()),
        token_breakdown=breakdown,
        provider_kind="vllm",
        model="m",
        span_count=span_count,
        first_span_ids=first or ["project-file:/x/MU.md", "tool-schema:read", "tool-schema:write"],
    )


def _write(events):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "session-1.jsonl")
    with open(p, "w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return p


class TestReplay(unittest.TestCase):
    def test_seed_plus_live_plus_compaction_matches_snapshot(self):
        # Seed: user, assistant(tool_call), tool_result, assistant  -> idx 0..3
        seed = [
            {"role": "user", "content": "a" * 40},
            {
                "role": "assistant",
                "content": [{"type": "tool_call", "id": "c1"}],
                "stop_reason": "tool_use",
            },
            {"role": "tool_result", "call_id": "c1", "content": "r" * 400, "is_error": False},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
            },
        ]
        events = [
            _ev(
                1,
                "session_created",
                provider_kind="vllm",
                model="m",
                usage_semantics={"cache_read_in_input": True},
            ),
            _ev(2, "continuation_seeded", predecessor_session_id="s0", messages=seed),
            _ev(3, "user_message", content="b" * 40),  # idx 4
            # rope: static + schemas + 5 msgs = 7 spans
            _assembly(
                4,
                1,
                5,
                7,
                {
                    "file_load": 100,
                    "tool_schema": 50,
                    "user": 20,
                    "assistant": 10,
                    "tool_result": 100,
                },
            ),
            _ev(
                5,
                "assistant_message_event",
                message={
                    "content": [{"type": "text", "text": "x"}],
                    "usage": {"input_tokens": 280, "cache_read_input_tokens": 200},
                },
            ),  # idx 5
            _ev(6, "user_message", content="c" * 40),  # idx 6
            # compaction drops the seeded tool cluster (idx 1,2) before call 2
            _ev(
                7,
                "compaction_assembly",
                model_call_id=2,
                policy_id="span-family-drop",
                tokens_before=300,
                tokens_after=200,
                decisions=[
                    {"action": "dropped", "span_id": "msg-1-assistant", "reason": "orphaned"},
                    {
                        "action": "dropped",
                        "span_id": "msg-2-tool-result:c1",
                        "reason": "old cluster",
                    },
                ],
                wall_clock_us=1,
            ),
            # 7 msgs total, 5 live -> 2 + 5 = 7 spans
            _assembly(
                8, 2, 7, 7, {"file_load": 100, "tool_schema": 50, "user": 30, "assistant": 10}
            ),
            _ev(
                9,
                "assistant_message_event",
                message={
                    "content": [{"type": "text", "text": "y"}],
                    "usage": {"input_tokens": 190, "cache_read_input_tokens": 150},
                },
            ),
            _ev(10, "context_cleared", reason="test"),
            _ev(11, "user_message", content="d"),  # idx 0 again
            _assembly(12, 3, 1, 3, {"file_load": 100, "tool_schema": 50, "user": 1}),
        ]
        rows = cs.simulate_session(_write(events))
        self.assertEqual([r.call_id for r in rows], [1, 2, 3])
        self.assertTrue(all(r.recon_ok for r in rows), [r.recon_ok for r in rows])
        self.assertEqual([r.spans for r in rows], [7, 7, 3])
        self.assertEqual([r.compaction for r in rows], [False, True, False])
        self.assertTrue(rows[0].first_call)
        self.assertTrue(rows[2].first_call, "context_cleared must reset the previous rope")
        # usage semantics: cache_read_in_input=True -> uncached = input - cache_read
        self.assertEqual(rows[0].reported_prompt, 280)
        self.assertEqual(rows[0].reported_uncached, 80)
        self.assertEqual(rows[1].reported_uncached, 40)


class TestCacheModels(unittest.TestCase):
    def _seg(self, i, tok, h=None):
        return cs.Seg(f"s{i}", h or f"h{i}", "user", tok)

    def test_flat_vs_segment_on_interior_drop(self):
        prev = [self._seg(i, 10) for i in range(6)]  # 60 tokens
        cur = [
            prev[0],
            prev[1],
            prev[3],
            prev[4],
            prev[5],
            self._seg(6, 7),
        ]  # dropped s2, appended s6
        flat = cs.Scenario("flat", 0, "prev")
        seg6 = cs.Scenario("seg6", 6, "prev")
        seg1 = cs.Scenario("seg1", 1, "prev")
        resident = {s.key for s in prev}
        # flat: prefix hit s0,s1 (20); re-prefill s3,s4,s5,s6 = 37
        self.assertEqual(cs.simulate_call(prev, cur, resident, flat), (37.0, 0.0))
        # segments k=6: relocate s3,s4,s5 (30); prefill only s6 = 7
        self.assertEqual(cs.simulate_call(prev, cur, resident, seg6), (7.0, 30.0))
        # k=1: s3,s4,s5 were contiguous before and after -> ONE run; same result
        self.assertEqual(cs.simulate_call(prev, cur, resident, seg1), (7.0, 30.0))

    def test_k_counts_runs_not_spans(self):
        prev = [self._seg(i, 10) for i in range(8)]
        # drop s1 and s5 -> survivors form runs [s2,s3,s4] (30) and [s6,s7] (20)
        cur = [prev[0], prev[2], prev[3], prev[4], prev[6], prev[7]]
        resident = {s.key for s in prev}
        seg1 = cs.Scenario("seg1", 1, "prev")
        seg2 = cs.Scenario("seg2", 2, "prev")
        # k=1 relocates the largest run only
        self.assertEqual(cs.simulate_call(prev, cur, resident, seg1), (20.0, 30.0))
        self.assertEqual(cs.simulate_call(prev, cur, resident, seg2), (0.0, 50.0))

    def test_first_call_is_all_prefill(self):
        cur = [self._seg(0, 5), self._seg(1, 5)]
        self.assertEqual(
            cs.simulate_call(None, cur, set(), cs.Scenario("flat", 0, "prev")), (10.0, 0.0)
        )

    def test_content_change_under_stable_id_breaks_prefix(self):
        prev = [self._seg(0, 10), self._seg(1, 10), self._seg(2, 10)]
        cur = [prev[0], self._seg(1, 10, h="mutated"), prev[2]]
        resident = {s.key for s in prev}
        flat = cs.Scenario("flat", 0, "prev")
        seg = cs.Scenario("seg", INF, "prev")
        self.assertEqual(cs.simulate_call(prev, cur, resident, flat), (20.0, 0.0))
        self.assertEqual(cs.simulate_call(prev, cur, resident, seg), (10.0, 10.0))

    def test_session_residency_recovers_a_recalled_span(self):
        a, b, c = self._seg(0, 10), self._seg(1, 10), self._seg(2, 10)
        seg_prev = cs.Scenario("p", INF, "prev")
        seg_sess = cs.Scenario("s", INF, "session")
        # call1: [a,b,c]; call2: [a,c] (b evicted); call3: [a,c,b] (b recalled)
        res_prev = {s.key for s in [a, c]}  # previous call only
        res_sess = {s.key for s in [a, b, c]}  # everything ever sent
        self.assertEqual(cs.simulate_call([a, c], [a, c, b], res_prev, seg_prev), (10.0, 0.0))
        self.assertEqual(cs.simulate_call([a, c], [a, c, b], res_sess, seg_sess), (0.0, 10.0))


class TestAggregate(unittest.TestCase):
    def test_savings_and_validation_roll_up(self):
        r1 = cs.CallRow(
            "s",
            1,
            "vllm",
            "m",
            3,
            100,
            False,
            False,
            True,
            {"flat": 100, "x": 100},
            {"flat": 0, "x": 0},
            100,
            100,
        )
        r2 = cs.CallRow(
            "s",
            2,
            "vllm",
            "m",
            3,
            100,
            True,
            False,
            False,
            {"flat": 50, "x": 10},
            {"flat": 0, "x": 40},
            100,
            45,
        )
        agg = cs.aggregate([r1, r2])
        f = agg["fleet"]
        self.assertEqual(f["calls"], 2)
        self.assertEqual(f["compaction_calls"], 1)
        self.assertEqual(f["prefill"], {"flat": 150, "x": 110})
        self.assertAlmostEqual(f["savings_vs_flat"]["x"], 1 - 110 / 150, places=4)
        # first call excluded from validation (nothing to cache yet)
        self.assertEqual(f["validation"]["calls"], 1)
        self.assertEqual(f["validation"]["sim_flat_uncached"], 50)
        self.assertEqual(f["validation"]["reported_uncached"], 45)
        self.assertEqual(agg["compaction_calls"]["calls"], 1)


if __name__ == "__main__":
    unittest.main()
