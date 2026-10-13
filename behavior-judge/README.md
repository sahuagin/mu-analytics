# Semantic behavior-judge for agent transcripts

Detecting when an AI coding agent has *misbehaved* — by having a separate LLM **read the
session transcript for meaning**, not by matching strings or watching metrics. The cheap
signals (syntactic anti-patterns, frustration-keyword rates) are confounded dead-ends; the
failures that matter are **semantic** and have no syntactic or telemetry fingerprint. So we
build an LLM **behavior-judge** with a per-class rubric and a verbatim-evidence requirement.

## Contents
- **[`METHODOLOGY.md`](METHODOLOGY.md)** — the approach, the findings, and the validation
  (100% recall / 0% FP on a held-out set, with the honest caveats: presumed-clean negatives
  leak, the judge is non-deterministic → majority-vote, calibration is per-harness).
- **[`judge/`](judge/)** — the judge: system-prompt template + the 5-class rubric
  (`false_success`, `map_as_terrain`, `scope_overreach`, `relitigation`, `dismissiveness`).
- **[`scripts/`](scripts/)** — `render_transcript.py` (cc/mu JSONL → turn-numbered text),
  `run_judge.py` / `run_judge_batch.py` (the judge runners), and `behavior_rates.py` +
  `frustration_lift.py` (the syntactic + frustration baselines — included to *show* they're
  dead ends).
- **[`RELATED-WORK.md`](RELATED-WORK.md)** — a cited survey of public hook frameworks,
  directive practices, and the limits-of-hooks literature.
- **[`AGENTS.md`](AGENTS.md)** — the publishing rules this research line follows.

## Quickstart
```
# 1. render a session → turn-numbered transcript
python3 scripts/render_transcript.py path/to/session.jsonl > t.txt
# 2. judge it for one behavior class (needs an ollama endpoint; --host/--model configurable)
python3 scripts/run_judge.py --transcript t.txt --cls dismissiveness
# 3. batch over a manifest of session_refs
python3 scripts/run_judge_batch.py --manifest sessions.tsv --out results.jsonl
```

## Pointwise result versions

The root-level `pointwise_judge.py` / `pointwise_driver.py` store results per
session chunk, analyzer, analyzer version, and model target. Analyzer versions
hash the pipeline version, shared prompt template, class identifier, and the
**selected class rubric**, matching the rubric used by `run_judge.py`.
Adding or editing a different class therefore does not invalidate completed
results for unchanged analyzers. Changes to the selected rubric, shared prompt,
or pipeline version do require reprocessing.

**Transition from whole-file rubric hashes:** this changes the hash format once.
Existing rows are retained, but their old versions do not match the new keys;
they are not automatically reused. There is no automatic historical migration.
Before running a large backfill, account for this one-time reprocessing, or
perform a separately reviewed migration that proves the old and new effective
analyzer inputs are equivalent. Do not blindly relabel historical results as
current. The new class-scoped behavior applies to results keyed with the new
format.

## Scope
This is the **generic methodology + detectors + survey of public prior art**. It contains **no
session logs, no transcripts, no operator data, and no defensive-tooling config** — by design
(see `AGENTS.md`). Licensed under the repository's license; third-party work is *cited, not
copied* (see `RELATED-WORK.md`).
