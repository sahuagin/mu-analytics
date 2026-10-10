"""The judge's no-verdict path: every rank that gives no verdict is classified by its exit
code, the reason reaches the log per rank, and the run's closing line counts verdicts
written against expected (mu-qmnoo). Hermetic — no model, no dispatcher, no store."""

import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import judge_incremental  # noqa: E402


def _load_run_judge():
    path = os.path.join(ROOT, "behavior-judge", "scripts", "run_judge.py")
    spec = importlib.util.spec_from_file_location("run_judge", path)
    assert spec is not None and spec.loader is not None, path
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


run_judge = _load_run_judge()


class AttemptKind(unittest.TestCase):
    def test_exit_code_decides_the_kind(self):
        self.assertEqual(run_judge.attempt_kind(4, "", None), "out_of_tokens")
        self.assertEqual(run_judge.attempt_kind(124, "", None), "timeout")
        self.assertEqual(run_judge.attempt_kind(75, "", None), "skipped")
        self.assertEqual(run_judge.attempt_kind(1, "some text", None), "exit_1")

    def test_clean_exit_without_envelope_is_the_text_shaped_case(self):
        prose = "**false_success: occurred=false.** The assistant ran the tests."
        self.assertIsNone(run_judge.coerce_json(prose))
        self.assertEqual(run_judge.attempt_kind(0, prose, None), "no_json")
        self.assertEqual(run_judge.attempt_kind(0, "", None), "empty_reply")

    def test_a_verdict_is_a_verdict_whatever_the_exit(self):
        self.assertEqual(run_judge.attempt_kind(0, "{}", {"occurred": False}), "verdict")

    def test_json_without_a_boolean_occurred_is_not_a_verdict(self):
        self.assertTrue(run_judge.is_verdict({"occurred": True, "evidence": []}))
        self.assertFalse(run_judge.is_verdict({"occurred": None}))
        self.assertFalse(run_judge.is_verdict({"behavior": "x", "summary": "no field"}))
        self.assertFalse(run_judge.is_verdict(["not", "a", "dict"]))
        self.assertEqual(run_judge.attempt_kind(0, '{"a": 1}', None, {"a": 1}), "bad_envelope")

    def test_required_provider_is_rank_zero_and_disables_fallthrough(self):
        ladder = [("flashnext", "local"), ("remote", "fallback")]
        self.assertEqual(
            run_judge.constrain_ladder(ladder, required_provider="flashnext"),
            [("flashnext", "local")],
        )
        with self.assertRaises(ValueError):
            run_judge.constrain_ladder(ladder, required_provider="remote")
        with self.assertRaises(ValueError):
            run_judge.constrain_ladder([], required_provider="flashnext")

    def test_single_rank_without_provider_constraint(self):
        ladder = [("one", "a"), ("two", "b")]
        self.assertEqual(run_judge.constrain_ladder(ladder, single_rank=True), [ladder[0]])
        self.assertEqual(run_judge.constrain_ladder(ladder), ladder)

    def test_rubric_at_tail_keeps_transcript_prefix_stable_across_classes(self):
        template = "system {CLASS_RUBRIC}"
        transcript = "[001] USER: same chunk"
        s1, u1 = run_judge.build_prompt_parts(template, "rubric one", transcript, True)
        s2, u2 = run_judge.build_prompt_parts(template, "rubric two", transcript, True)
        self.assertEqual(s1, s2)
        self.assertTrue(u1.startswith(transcript))
        self.assertTrue(u2.startswith(transcript))
        self.assertIn("rubric one", u1)
        self.assertIn("rubric two", u2)
        self.assertNotEqual(u1, u2)

    def test_correction_overgeneralization_has_balanced_calibration_cases(self):
        rubric = run_judge.class_rubric("correction_overgeneralization")
        self.assertIn("situational correction", rubric)
        self.assertIn("durable rule", rubric)
        path = os.path.join(
            ROOT,
            "behavior-judge",
            "calibration",
            "correction_overgeneralization.json",
        )
        with open(path) as f:
            cases = json.load(f)
        self.assertEqual(len(cases), 10)
        self.assertEqual(sum(case["expected"] is True for case in cases), 5)
        self.assertEqual(sum(case["expected"] is False for case in cases), 5)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        self.assertTrue(all(case["transcript"].startswith("[001]") for case in cases))


class ClassifyFailure(unittest.TestCase):
    def test_structured_record_gives_one_line_per_rank_with_the_reason(self):
        stdout = (
            '{"behavior": "false_success", "occurred": null, "attempts": ['
            '{"provider": "openai-codex", "model": "m1", "rc": 0, "kind": "no_json", '
            '"secs": 41, "reason": "reply began: \'**false_success: occurred=false.**\'"}, '
            '{"provider": "claude-oauth", "model": "m2", "rc": 4, "kind": "out_of_tokens", '
            '"secs": 3, "reason": "agent-dispatch: claude-oauth/m2 is OUT OF TOKENS (exit 4)"}'
            "]}"
        )
        kinds, lines = judge_incremental.classify_failure(stdout, "judge: no target ...")
        self.assertEqual(kinds, ["no_json", "out_of_tokens"])
        self.assertEqual(len(lines), 2)
        self.assertIn("openai-codex/m1: no_json (exit 0, 41s) — reply began:", lines[0])
        self.assertIn("claude-oauth/m2: out_of_tokens (exit 4, 3s) — agent-dispatch:", lines[1])

    def test_no_record_falls_back_to_the_stderr_tail(self):
        stderr = "Traceback (most recent call last):\n  File x\nKeyError: 'boom'\n"
        kinds, lines = judge_incremental.classify_failure("", stderr)
        self.assertEqual(kinds, ["runner_error"])
        self.assertEqual(lines[-1], "KeyError: 'boom'")

    def test_nothing_at_all_still_says_so(self):
        kinds, lines = judge_incremental.classify_failure("", "")
        self.assertEqual(kinds, ["runner_error"])
        self.assertEqual(lines, ["(no stderr; empty stdout)"])


class RunSummary(unittest.TestCase):
    def test_counts_written_against_expected_and_tallies_kinds(self):
        state = {
            "judged": 2,
            "skipped": 1,
            "expected": 24,
            "written": 16,
            "failures": {"no_json": 5, "out_of_tokens": 8},
        }
        line = judge_incremental.run_summary(state)
        self.assertIn("judged 2, skipped 1", line)
        self.assertIn("verdicts written 16 of 24 expected", line)
        self.assertIn("no-verdict ranks: out_of_tokens 8, no_json 5", line)

    def test_clean_run_has_no_tally(self):
        state = {"judged": 3, "skipped": 0, "expected": 24, "written": 24, "failures": {}}
        line = judge_incremental.run_summary(state)
        self.assertIn("verdicts written 24 of 24 expected", line)
        self.assertNotIn("no-verdict", line)


if __name__ == "__main__":
    unittest.main()
