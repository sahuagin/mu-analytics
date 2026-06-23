# HANDOFF — directive-adherence / context-rot / enforcement study

For the next session. Written 2026-06-22 at ~80% context before a restart.

## Mission
Why does **cc** ignore loaded directives (CLAUDE.md/AGENTS.md) that it used to follow,
while **mu** (minimal initial context + point-of-use `discover`) adheres more often?
Measure adherence over the session corpus, then test **enforcement (hooks)** —
**one at a time, measure-first, before/after, rip out anything with no material gain,
never accumulate from hope.** Hooks > docs because the harness runs them regardless of
model attention; docs decay (esp. under heavy context).

## ACCESS — read this first (the prior session got it wrong)
- **`.172` IS reachable: `ssh tcovert@10.1.1.172` (key auth, no agent needed).**
- **Full corpus = `.172:~/ai-sessions/{claude,mu}` — 8,654 jsonl** (both fleets, both
  machines, historic). The local jail subset (~600, recent/short/bench-heavy) is NOT
  representative — **run everything on `.172`, not locally.**
- Deployed analytics on `.172`: `~/src/public_github/mu-analytics` (running, duckdb in
  `~/.duckdb`, refreshed q15m), `~/mu-stats/` (e.g. `mu-audit-findings.tsv`).
- Postmortems: local `~/.claude/notes` (copies) AND `.172:~/.claude-personal/notes` (8).
- The jail is isolated FS-wise but reaches `.172` **services** (etcd, beadsd :7771/:7772,
  mu MCP :7622/:7740) AND **ssh**. No `~/ai-sessions` mount locally.

## Where the work lives
Workspace **`mu-analytics-directive-adherence`** (jj, in the mu-analytics repo). 6 committed
rounds (NOT pushed — review with `jj log`):
- r1 `trxuwmzu` context-rot probe + research log
- r2 `rkwqmzwp` violation classifiers + directive timeline
- r3 `mnpsvzwv` heredoc/shell-write classifiers
- r4 `toymrovw` FP-audit + refine
- r5 `lorkqxvk` outcome join, exposure-normalized
- r6 `pkppsvuo` windowed/temporal + convergence
Research log: **`docs/directive-adherence.md`** (full findings + hypotheses H1–H5).
Scripts (STANDALONE raw-jsonl prototypes over the LOCAL subset — graduate to the `ev`
view / run on `.172`): `scripts/{adherence_probe,violations,outcome,windowed}.py`.

## Validated (durable) findings
- **Structural disparity:** mu ~5k vs cc ~27k median initial context (Anthropic base ~19k
  + operator additions ~8k). The mu-vs-cc signal-to-noise gap, quantified.
- **`heredoc` is the one signal that survived exposure-normalization** (~2.9× per-message
  operator-frustration; cc ~¼–⅓ of tool sessions vs mu ~0). Strongest enforcement candidate;
  fix is uncontroversial (Write tool); operator independently flagged it.
- **Methodology lessons (the real value):**
  1. Only PURE tool-stream predicates classify cleanly from logs (force_push, edit_loop,
     heredoc, large_bash). CONTEXT-STATE predicates (edit_before_read, missing-discovery)
     over-count retroactively → measure/enforce at RUNTIME (hooks), not the log sweep.
  2. PRESENCE outcomes are exposure confounds (long sessions → ≥1 marker near-certain).
     Use per-message RATE + size control (or degradation.py's regression).
  3. Separate STEER (normal directive: "stop","no, do X") from REWORK (degradation:
     redo/again/we-already-did/making-more-work). Operator language is mostly steering.
  4. Sessions have WINDOWS, not one good/bad label → analyze within-session/temporal.

## Did NOT survive
- **H4 (directive-following degrades with context depth) — unsupported** once exposure-
  normalized in the local data (context→frustration was non-monotonic). Re-test with power
  on `.172`; do not assume context-rot is the cause.
- Naive violation×frustration lifts (~2–3×) were session-length confounds.

## NEXT STEPS (priority order)
1. **Move to `.172`.** ssh in; run the analyses over `~/ai-sessions` (8,654) with POWER.
   Prefer graduating the signals into `features.py` + `degradation.py` (multivariate,
   length-controlled) over the raw-jsonl prototypes — that's the proper test of everything
   underpowered locally (heredoc, context-rot, outcome correlations).
2. **Mine the postmortems for ground truth + markers.** The 8 incidents document real
   degradations with specific markers/behaviors and likely session refs. Start with
   `incident-2026-06-20-discovery-bypass-jj-rederivation-spiral.md` (THE directive-ignoring
   spiral), `incident-2026-06-19-ollama-gpu-thrash.md`, `frustration-scan-2026-06-12.md`,
   `claude-capacity-incidents.md`. Extract their markers → detector library; find the
   referenced sessions → ground-truth labels to validate detectors against.
3. **Windowed/temporal on `.172`.** The long all-day sessions (good-open → REWORK arc →
   ~4-5am exhaustion-leave → gap → recovery-probe) live there, not locally. Run
   `windowed.py` (refine REWORK/PROBE markers from the postmortems); detect the
   degradation TRANSITION per session + the gap leave/return signature.
4. **Enforcement loop (only after a classifier is outcome-validated):** heredoc is the lead.
   Baseline exists → one `PreToolUse(Bash)` hook (warn/block heredoc-file-writes + oversized
   commands) → A/B with **self-breadcrumb** (hook emits a marker so treatment state is in the
   log) + **none→doc tiering** via the directive timeline (cc base 2026-06-18 + today's adds;
   mu base 2026-06-09/10 + today's). Outcome = quality (marks/sentiment); violation = mechanism
   check (→0 under a blocking hook). The none→doc rung is runnable retroactively NOW on `.172`.

## Operating constraints (carry forward)
- Per-turn jj commit (`jj describe` + `jj new`); **no PRs** (operator reviews the stack).
- Benchmarks (`bench` in path) excluded by default.
- Heredoc note was a DATA signal, not a behavior leash — but Write/Edit for durable files is
  good practice (auditable + clobber-safe).
- `agent-role` resolves models by role (don't hardcode); `discover`/`t4c` to find tools.

## Parked, unrelated work (this session, unfinished)
- **mu LOTO** (workspace `mu-mu-0pqk`): `with-ollama-lease` built+validated+tracked
  (`scripts/with-ollama-lease.sh`, symlinked); `agent-slot` default fixed → `.172`. TODO:
  wire the lease into panel (`dispatch.sh`/`consensus.sh`) + leaf (`ai-review.sh`) + launcher
  (`agent-dispatch.sh`), add `glm-5.2`+`qwen3.7-plus` fallback ranks to `code_review`.
- **ai-review cleanup** (workspace `mu-ai-review-modelcfg`): 3 commits, review-ready, unshipped.
- **agent-slot home**: vendored on `agent_tools-local-tooling` branch `agent/local-tooling-2026-06-21` (unpushed).
