import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import demo_data  # noqa: E402
import sample_data  # noqa: E402

# Keys the proto template consumes — demo_data and the real build() must emit them.
_CONTRACT_KEYS = {
    "as_of",
    "note",
    "kpi",
    "cost_by_kind",
    "cost_by_fleet",
    "cost_by_model",
    "outcomes",
    "cost_composition_top_session",
    "top_sessions",
    "all_sessions",
    "hallucination_by_model",
    "trend_by_day",
    "marks",
    "flagged_queue",
    "compaction",
    "context_trajectory",
    "context_compactions",
    "tool_mix",
    "recall",
    "cache_econ",
    "per_ask_sessions",
    "meta",
    "degradation_rate",
}


class TestPureFns(unittest.TestCase):
    def test_rate_key_strips_provider_prefix(self):
        self.assertEqual(sample_data.rate_key("anthropic/claude-opus-4-8"), "claude-opus-4-8")

    def test_rate_key_longest_prefix_match_on_date_suffix(self):
        self.assertEqual(sample_data.rate_key("claude-haiku-4-5-20251001"), "claude-haiku-4-5")

    def test_rate_key_unknown_and_empty(self):
        self.assertIsNone(sample_data.rate_key("totally-unknown-model"))
        self.assertIsNone(sample_data.rate_key(""))
        self.assertIsNone(sample_data.rate_key(None))

    def test_cost_kind(self):
        self.assertEqual(sample_data.cost_kind("anthropic_api", "claude-opus-4-8"), "billed")
        self.assertEqual(sample_data.cost_kind("ollama", "gemma"), "free")
        self.assertEqual(sample_data.cost_kind("openai_codex", "gpt-5.5"), "subscription")
        self.assertEqual(sample_data.cost_kind("", ""), "free")  # empty model is free

    def test_short_id_is_stable_and_prefixed(self):
        a = sample_data._short_id("mu", "task-1")
        self.assertEqual(a, sample_data._short_id("mu", "task-1"))  # deterministic
        self.assertTrue(a.startswith("mu·"))
        self.assertEqual(len(a.split("·")[1]), 8)  # 32-bit hex (was 16-bit/4-hex; collided)
        self.assertNotEqual(a, sample_data._short_id("mu", "task-2"))

    def test_day_format(self):
        self.assertRegex(sample_data._day(1_700_000_000_000), r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(sample_data._day(None), r"^\d{4}-\d{2}-\d{2}$")  # 0 -> epoch, no crash


class TestSessionize(unittest.TestCase):
    def _task(self, tid, cost, **kw):
        r = {
            "task_id": tid,
            "fleet": "mu",
            "model": "claude-opus-4-8",
            "provider": "openai_codex",
            "inp": 10,
            "out": 5,
            "cr": 100,
            "cw": 20,
            "cost": cost,
            "tools": 3,
            "outcome_class": "clean_success",
            "started_at_unix_ms": 1000,
            "ended_at_unix_ms": 2000,
        }
        r.update(kw)
        return r

    def test_groups_tasks_into_one_session_summing_cost(self):
        rows = [self._task("t1", 1.0), self._task("t2", 2.0, started_at_unix_ms=3000)]
        sessions = [
            {
                "daemon": "d1",
                "sid": "session-1",
                "started_ms": 500,
                "model": "claude-opus-4-8",
                "task_ids": ["t1", "t2"],
                "tool_calls": 42,
                "is_child": True,
            }
        ]
        out = sample_data._sessionize_mu(rows, sessions)
        self.assertEqual(len(out), 1)  # two tasks -> one session
        s = out[0]
        self.assertEqual(s["task_id"], "d1/session-1")  # unique session key
        self.assertEqual(s["cost"], 3.0)  # summed
        self.assertEqual(s["tools"], 42)  # event-log tool count, not sink sum
        self.assertEqual(s["started_at_unix_ms"], 500)  # event-log start
        self.assertTrue(s["is_child"])
        self.assertEqual(s["outcome_class"], "clean_success")  # last task's outcome

    def test_unmapped_task_survives_as_its_own_row(self):
        rows = [self._task("t1", 1.0), self._task("orphan", 9.0)]
        sessions = [
            {
                "daemon": "d1",
                "sid": "session-1",
                "started_ms": 500,
                "model": "claude-opus-4-8",
                "task_ids": ["t1"],
                "tool_calls": 1,
                "is_child": False,
            }
        ]
        out = sample_data._sessionize_mu(rows, sessions)
        self.assertEqual(len(out), 2)  # session + orphan passthrough
        self.assertEqual({r["cost"] for r in out}, {1.0, 9.0})  # no cost dropped


class TestDemoContract(unittest.TestCase):
    def test_demo_build_emits_full_contract(self):
        d = demo_data.build()
        missing = _CONTRACT_KEYS - set(d.keys())
        self.assertEqual(missing, set(), f"demo_data missing keys: {missing}")
        self.assertIsInstance(d["marks"], list)
        self.assertIn("flags", d["meta"])
        self.assertIn("mu", d["compaction"])
        self.assertIsInstance(d["degradation_rate"], (int, float))


if __name__ == "__main__":
    unittest.main()
