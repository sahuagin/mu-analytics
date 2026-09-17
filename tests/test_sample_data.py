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
    "session_index",
    "hallucination_by_model",
    "trend_by_day",
    "marks",
    "flagged_queue",
    "flagged_queue_total",
    "compaction",
    "context_trajectory",
    "context_compactions",
    "tool_mix",
    "recall",
    "cache_econ",
    "per_ask_sessions",
    "meta",
    "degradation_rate",
    "degradation_probe",
    "audit_findings",
    "delegations",
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

    def test_cached_input_is_a_subset_for_openai_and_disjoint_for_anthropic(self):
        # mu-hx0ta: OpenAI reports prompt_tokens as the whole prompt with the
        # cached tokens inside it; Anthropic reports disjoint buckets. The same
        # numbers must price differently, and the OpenAI cached token at 0.10x
        # of the input rate, not 1.10x (the mu #626 board finding).
        self.assertTrue(sample_data.cache_in_input("openai_codex"))
        self.assertTrue(sample_data.cache_in_input("openai_api"))
        self.assertFalse(sample_data.cache_in_input("anthropic_api"))
        self.assertFalse(sample_data.cache_in_input("nobody"))
        gpt = sample_data.RATES["gpt-6-astra"]
        # 55,577 prompt of which 37,632 cached, 1,200 out:
        # fresh 17,945 x $10 + cached 37,632 x $1 + out 1,200 x $50 = $0.2771
        got = sample_data.priced_cost("openai_codex", "gpt-6-astra", 55_577, 1_200, 37_632, 0)
        self.assertAlmostEqual(
            got,
            round(
                (17_945 * gpt["input"] + 37_632 * gpt["input"] * 0.10 + 1_200 * gpt["output"])
                / 1e6,
                4,
            ),
        )
        self.assertAlmostEqual(got, 0.2771, places=4)
        # the Anthropic rule on an Anthropic card prices the input in full
        opus = sample_data.RATES["claude-opus-4-8"]
        got_a = sample_data.priced_cost(
            "anthropic_api", "claude-opus-4-8", 55_577, 1_200, 37_632, 0
        )
        self.assertAlmostEqual(
            got_a,
            round(
                (55_577 * opus["input"] + 37_632 * opus["input"] * 0.10 + 1_200 * opus["output"])
                / 1e6,
                4,
            ),
        )
        # a fully cached OpenAI prompt costs the cached rate only; cached >
        # input (an inconsistent sample) never goes negative
        self.assertAlmostEqual(
            sample_data.priced_cost("openai_codex", "gpt-6-astra", 100_000, 0, 100_000, 0),
            0.10,
            places=4,
        )
        self.assertAlmostEqual(
            sample_data.priced_cost("openai_codex", "gpt-6-astra", 10, 0, 50, 0),
            round(50 * gpt["input"] * 0.10 / 1e6, 4),
        )
        # OpenAI's cache WRITES are inside the prompt total too (mu's
        # UsageSemantics::openai_style sets cache_creation_in_input): a written
        # token bills once at the write modifier, never also as fresh input.
        # 10k prompt = 1k fresh + 4k read + 5k written on a $10 card:
        # 0.01 + 4k x $1 + 5k x $12.5 = 0.0765, not 0.1265 (the mu board's
        # round-3 finding, the same double charge in this formula).
        comp = sample_data.cost_components("openai_api", "gpt-6-astra", 10_000, 0, 4_000, 5_000)
        self.assertAlmostEqual(comp["input"], 1_000 * gpt["input"] / 1e6)
        self.assertAlmostEqual(
            comp["cache_write"], 5_000 * gpt["input"] * sample_data.MULT["write_5m"] / 1e6
        )
        self.assertAlmostEqual(
            sample_data.priced_cost("openai_api", "gpt-6-astra", 10_000, 0, 4_000, 5_000), 0.0765
        )
        # a prompt that is entirely written prices as writes alone
        self.assertAlmostEqual(
            sample_data.priced_cost("openai_api", "gpt-6-astra", 10_000, 0, 0, 10_000), 0.125
        )
        # Anthropic's written tokens are a separate bucket: fresh input stays
        self.assertAlmostEqual(
            sample_data.cost_components("anthropic_api", "gpt-6-astra", 10_000, 0, 0, 10_000)[
                "input"
            ],
            10_000 * gpt["input"] / 1e6,
        )
        # unlisted models flag as 0.0, never guessed
        self.assertEqual(sample_data.priced_cost("openai_codex", "gpt-9-nope", 1000, 10, 0, 0), 0.0)
        # gpt-6-astra is rated (it was unpriced since the 2026-09-09 roster move)
        self.assertEqual(sample_data.rate_key("gpt-6-astra"), "gpt-6-astra")

    def test_producer_cost_wins_over_the_rate_card_and_older_sinks_still_price(self):
        # mu prices each task per model call at telemetry time (tasks.cost_usd,
        # mu-hx0ta) — the only place gpt-6-astra's per-request long-context
        # tier is exact. The loader takes that figure when the sink has it
        # and prices from totals (base rate) only when it does not.
        import sqlite3
        import tempfile

        schema = (
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, session_id TEXT, provider TEXT, "
            "model TEXT, exit_reason TEXT, outcome_class TEXT, tool_call_count INTEGER, "
            "prompt_tokens INTEGER, completion_tokens INTEGER, cache_read_tokens INTEGER, "
            "cache_write_tokens INTEGER, started_at_unix_ms INTEGER, ended_at_unix_ms INTEGER{extra})"
        )
        cols = "task_id, session_id, provider, model, exit_reason, outcome_class, tool_call_count, "
        cols += "prompt_tokens, completion_tokens, cache_read_tokens, cache_write_tokens, "
        cols += "started_at_unix_ms, ended_at_unix_ms"
        with tempfile.TemporaryDirectory() as tmp:
            new = os.path.join(tmp, "new.sqlite")
            con = sqlite3.connect(new)
            con.execute(schema.format(extra=", cost_usd REAL"))
            # one 300k-prompt task: the producer saw a single 300k call and
            # applied the tier ($6.00); the base rate on the totals is $3.00
            con.execute(
                f"INSERT INTO tasks ({cols}, cost_usd) VALUES "
                "('t1','s1','openai_api','gpt-6-astra','done','ok',0,300000,0,0,0,1,2,6.0)"
            )
            # a task the producer could not price (NULL) falls back to the card
            con.execute(
                f"INSERT INTO tasks ({cols}, cost_usd) VALUES "
                "('t2','s2','openai_api','gpt-6-astra','done','ok',0,1000,0,0,0,1,2,NULL)"
            )
            con.commit()
            con.close()
            rows = {r["task_id"]: r for r in sample_data._load("mu", new)}
            self.assertEqual(rows["t1"]["cost"], 6.0)
            self.assertEqual(rows["t2"]["cost"], 0.01)

            old = os.path.join(tmp, "old.sqlite")
            con = sqlite3.connect(old)
            con.execute(schema.format(extra=""))
            con.execute(
                f"INSERT INTO tasks ({cols}) VALUES "
                "('t1','s1','openai_api','gpt-6-astra','done','ok',0,300000,0,0,0,1,2)"
            )
            con.commit()
            con.close()
            rows = {r["task_id"]: r for r in sample_data._load("mu", old)}
            # no column: the base-rate figure, never a crash
            self.assertEqual(rows["t1"]["cost"], 3.0)

    def test_short_id_is_stable_and_prefixed(self):
        a = sample_data._short_id("mu", "task-1")
        self.assertEqual(a, sample_data._short_id("mu", "task-1"))  # deterministic
        self.assertTrue(a.startswith("mu·"))
        self.assertEqual(len(a.split("·")[1]), 8)  # 32-bit hex (was 16-bit/4-hex; collided)
        self.assertNotEqual(a, sample_data._short_id("mu", "task-2"))

    def test_row_identity_includes_canonical_and_legacy_aliases(self):
        rid, ref, aliases = sample_data._row_identity(
            {
                "fleet": "mu",
                "task_id": "d1234567/s1",
                "daemon": "d1234567",
                "sid": "s1",
                "ref": "mu:d1234567:s1",
            }
        )
        self.assertTrue(rid.startswith("mu·"))
        self.assertEqual(ref, "mu:d1234567:s1")
        self.assertIn("mu·d123", aliases)
        self.assertIn("d1234567/s1", aliases)
        self.assertIn("s1", aliases)

    def test_cc_row_identity_uses_event_log_canonical_ref(self):
        rid, ref, aliases = sample_data._row_identity(
            {"fleet": "cc", "task_id": "cc-12345678-aaaa-bbbb-cccc-abcdefabcdef"}
        )
        self.assertTrue(rid.startswith("cc·"))
        self.assertEqual(ref, "cc:12345678-aaaa-bbbb-cccc-abcdefabcdef")
        self.assertIn("cc-12345678-aaaa-bbbb-cccc-abcdefabcdef", aliases)
        self.assertIn("cc:cc-12345678-aaaa-bbbb-cccc-abcdefabcdef", aliases)

    def test_day_format(self):
        self.assertRegex(sample_data._day(1_700_000_000_000), r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(sample_data._day(None), r"^\d{4}-\d{2}-\d{2}$")  # 0 -> epoch, no crash

    def test_dashboard_noise_policy_is_narrow(self):
        self.assertTrue(sample_data._is_dashboard_noise({"model": "faux"}))
        self.assertTrue(sample_data._is_dashboard_noise({"model": "FAUX"}))
        self.assertFalse(sample_data._is_dashboard_noise({"model": "qwen3-coder", "kind": "free"}))
        self.assertFalse(sample_data._is_dashboard_noise({"model": "", "kind": "free"}))


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
        self.assertEqual(s["ref"], "mu:d1:session-1")
        self.assertEqual(s["cost"], 3.0)  # summed
        self.assertEqual(s["tools"], 42)  # event-log tool count, not sink sum
        self.assertEqual(s["started_at_unix_ms"], 500)  # event-log start
        self.assertTrue(s["is_child"])
        self.assertEqual(s["outcome_class"], "clean_success")  # last task's outcome

    def test_marked_sessions_are_flagged(self):
        rows = [self._task("t1", 1.0)]
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
        display_id, ref, _aliases = sample_data._row_identity(out[0])
        flagged = display_id in {} or ref in {"mu:d1:session-1": {"rating": 2}}
        self.assertTrue(flagged)


class TestDemoContract(unittest.TestCase):
    def test_demo_build_emits_full_contract(self):
        d = demo_data.build()
        missing = _CONTRACT_KEYS - set(d.keys())
        self.assertEqual(missing, set(), f"demo_data missing keys: {missing}")
        self.assertIsInstance(d["marks"], list)
        self.assertIn("flags", d["meta"])
        self.assertIn("mark_summary", d["meta"])
        self.assertIn("mu", d["compaction"])
        self.assertIsInstance(d["degradation_rate"], (int, float))


class TestDegradationProbe(unittest.TestCase):
    """The fold: degradation-ml.json + mu-audit-findings.tsv -> DATA section."""

    def test_shapes_probe_and_audit(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ml = {
                "meta": {
                    "r2": 0.1,
                    "mae": 20.0,
                    "n_interactive": 2,
                    "n_unattended": 1,
                    "importances": [["input_tok", 0.4]],
                },
                "sessions": [
                    # interactive: pred>obs (unnoticed) and pred<obs (task_frust)
                    {"session_ref": "mu:d1:s1", "kind": "interactive", "obs": -10.0, "pred": 30.0},
                    {"session_ref": "cc:u2", "kind": "interactive", "obs": 50.0, "pred": -5.0},
                    {"session_ref": "mu:d3:s3", "kind": "unattended", "pred": -40.0},
                ],
            }
            with open(os.path.join(tmp, "degradation-ml.json"), "w") as f:
                json.dump(ml, f)
            with open(os.path.join(tmp, "mu-audit-findings.tsv"), "w") as f:
                f.write("session_ref\tfirst_ts\tseverity\tinvariant\tevent_id\tdetail\n")
                f.write("mu:d1:s1\t2026-06-15\tHigh\trepeated_identical_tool_call\t452\tstuck\n")
            out = sample_data._degradation_probe(tmp)

        dp = out["degradation_probe"]
        self.assertEqual(dp["r2"], 0.1)
        # unnoticed = highest resid (pred-obs); task_frust = lowest
        self.assertEqual(dp["unnoticed"][0]["session_ref"], "mu:d1:s1")  # resid +40
        self.assertEqual(dp["task_frust"][0]["session_ref"], "cc:u2")  # resid -55
        self.assertEqual(dp["unattended"][0]["session_ref"], "mu:d3:s3")
        # audit findings parsed by column
        self.assertEqual(len(out["audit_findings"]), 1)
        self.assertEqual(out["audit_findings"][0]["invariant"], "repeated_identical_tool_call")

    def test_missing_files_degrade_to_empty(self):
        out = sample_data._degradation_probe("/nonexistent/stats/dir")
        self.assertEqual(out["degradation_probe"], {})
        self.assertEqual(out["audit_findings"], [])


if __name__ == "__main__":
    unittest.main()
