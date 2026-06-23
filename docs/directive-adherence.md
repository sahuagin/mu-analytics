# Directive adherence & context rot — research log

**Question.** Why does cc ignore loaded directives (AGENTS.md/CLAUDE.md) that it
used to follow, while mu — minimal initial context + point-of-use discovery —
adheres more often? Can we *measure* adherence over the session corpus (cc + mu,
retroactively over ~/ai-sessions on .172), and use that to test enforcement
(hooks) one at a time, before/after, keeping only what materially helps?

Companion infra: `mu audit` (deterministic process-layer auditors), `mu mark`
(operator 1–5 labels), `features.py`/`degradation.py`/`anomaly_worklist.py`
(the ML substrate), `cc_telemetry.py` (cc→mu unification).

## Hypotheses (pre-registered 2026-06-22, before results)

- **H1 — enforcement asymmetry.** `edit-before-read` ≈ 0 on cc (tool hard-blocks
  it), > 0 on mu (no guard). If mu ≈ 0 too, the predicate is uninteresting.
- **H2 — `edited-before-isolating`** present on both fleets, higher on cc.
  *(Caveat discovered: this directive is brand-new, so it is ~universally
  "violated" historically — only meaningful relative to when it entered context;
  needs the directive-timeline tiering below.)*
- **H3 — docs barely move it.** none→doc-only shows only a small drop in violation
  rate and little quality change. (Most-expected-to-confirm.)
- **H4 — context-rot / disparity.** Violation probability rises with the
  **(system-context)/(user-context) disparity** and with depth/U-position; the
  disparity ratio predicts better than absolute session length.
- **H5 — rule half-life.** A directive's protective effect decays as context
  grows — helps in small contexts, loses utility past some size threshold.

## Data & method

Per-turn context size is logged on both fleets (extractability confirmed):
- **cc**: `usage.{input,cache_read,cache_creation}_tokens` per assistant turn.
- **mu**: `context_assembly.token_count_estimate` per model call.
Tool sequence: cc `tool_use` events / mu `tool_call.name`. Compaction marks: cc
`isCompactSummary`, mu explicit `compaction*` events (11k+). mu's compactor
(`crates/mu-core/src/context/compaction/heuristic.rs`) can be replayed for
deterministic trigger marks if ever needed.

Prototype: `scripts/adherence_probe.py` reads raw jsonl (no DuckDB/.172) over the
**local subset** for fast iteration. Graduates into `features.py` (over the `ev`
view) for the real run on .172.

## Findings — round 1 (local subset, 2026-06-22)

Context-size substrate (`scripts/adherence_probe.py`):

| fleet | sessions | initial ctx (med) | max ctx (med / p90 / max) | growth |
|---|---|---|---|---|
| cc-real | 92 | **27,001** | 51k / 731k / **999k** | 1.62× |
| cc-bench | 30 | 19,341 | 19.3k / 19.7k / 20k | 1.00× |
| mu | 358 | **5,068** | 14k / 46k / 245k | 1.90× |

Reads:
1. **The mu↔cc disparity is real and ~5×.** mu starts at **~5k**, cc-real at
   **~27k**. mu's directives live in ~5k of ~5k (dominant); cc's live in ~27k
   dominated by the Anthropic base — direct support for "minimal context adheres
   better." (Caveat: mu's `token_count_estimate` and cc's cache-token sum are
   different measures; the *magnitude* of the gap, not the exact ratio, is the
   signal.)
2. **Anthropic base ≈ 19k; operator additions ≈ +8k.** cc-bench (minimal) floors
   at ~19.3k; cc-real adds ~8k (CLAUDE.md/AGENTS.md/skills). So in cc the
   operator's directives are only ~30% of the starting context — ~70% is the
   harness. This is the H4 disparity, quantified, and it's the **common case**.
3. **Two distinct failure modes** (don't conflate): (a) *system-drowns-directives*
   — the median short session, disparity structural from turn 1 (H4); (b)
   *lost-in-the-middle* — only the cc deep tail (max → 999k), a minority (H5).
4. **Compaction detection is still weak** (1 cc, 0 mu via cache-drop) despite cc
   sessions reaching ~1M — likely cc compaction splits into a new transcript file
   (no intra-file drop), and mu's estimate doesn't dip the same way. **Switch to
   explicit events** (mu has them; cc `isCompactSummary`) and test the file-split
   hypothesis. Do not trust the cache-drop count yet.

## Directive-entry timeline (treatment substrate)

From the jj history of the doc files:
- **cc** (`~/.claude`): base config **2026-06-18**; pre-edit-gate + model-selection both **2026-06-22**.
- **mu** (`~/.config/mu`): base **2026-06-09/10** (initial + discover/skill calibration); model-selection **2026-06-22**.

Power note: the *recent* (today) directives have ~no "after" sessions yet, and the
none→doc shift needs the historic corpus on **.172** that straddles these dates.
Local mtimes are also unreliable (`stat` returned bad values). So **none→doc is
.172-bound**; locally we measure age-independent base rates instead.

## Findings — round 2 (violation base rates, local subset, 2026-06-22)

`scripts/violations.py` (benchmarks excluded), per session with tool calls:

| predicate | cc-real (85) | mu (357) |
|---|---|---|
| edit_before_read | 29% (Edit-only 27%) | 1% |
| edit_loop (≥5 edits, same file) | 23% | 0% |
| dangerous_bash (`rm -rf` etc.) | 10% | 0% |
| force_push / reset --hard | 2% | 0% |

The validation findings matter more than the raw rates:
1. **mu near-zeros are REAL, not a name artifact.** mu vocab: read 1391, grep 1063,
   bash 449, code_recall 254, discover 59, edit 56, write 5, `spawn_worker` 5. Its
   mix is investigative + native (discover/code_recall/memory_recall), so it shells
   bash + edits far less than cc. → normalize cross-fleet rates by **edit-bearing**
   sessions, not all.
2. **`edit_before_read` is a poor retroactive classifier.** Write-new (25) vs
   Edit-existing (23) split still leaves 27%, and even that over-counts: the rule cc
   enforces is "edit a file whose CONTENT isn't in context," and content enters via
   Grep/attachments/prior turns the tool-stream log doesn't expose. **H1 neither
   confirmed nor refuted** — needs runtime state we don't have in the log.
3. **Methodological rule:** classify cleanly only PURE tool-stream predicates
   (force_push, dangerous_bash[narrow], edit_loop). CONTEXT-STATE predicates
   (edit_before_read, missing-discovery) over-count from logs → enforce/measure
   them at RUNTIME (hooks), not in the log sweep.
4. `dangerous_bash` 10% = "ran an `rm -rf`-ish command," not necessarily dangerous
   (`rm -rf build/` is routine) — narrow before trusting.

Aside: mu's native `spawn_worker` (5 uses) confirms the spawn path is also a tool,
not only bash — ties back to the helper-spawn thread.

## Findings — round 3 (heredoc / shell-write anti-patterns, 2026-06-22)

Operator insight: cc frequently writes files via heredoc (`cat > f <<EOF`), runs code
via `python - <<PY`, and emits large bash that can't be audited in the approval dialog
(only a slice shows). Two harms: heredoc/shell writes **bypass the Write tool's
Read-before-overwrite guard** (overwrite-blindness — esp. under zsh `noclobber`), and
large heredocs are **unauditable** at approval time.

Pure tool-stream classifiers (`scripts/violations.py`), % of tool-bearing sessions:

| predicate | cc-real (85) | mu (357) |
|---|---|---|
| large_bash (>1200 chars) | 29% | 1% |
| shell_file_write (`cat/echo/tee > file`) | 28% | 0% |
| heredoc (`<<`) | 23% | 1% |
| code_in_heredoc (`python/node <<`) | 11% | 1% |

Reads:
- **cc-specific** (~¼–⅓ of cc tool sessions; ~0 on mu, which uses native edit/write +
  smaller bash). Cleanest, highest-signal, lowest-FP predicates so far.
- They **overlap/correlate** (a large heredoc file-write trips heredoc + shell_file_write
  + large_bash) — don't sum as independent.
- **Best first enforcement candidate:** the fix is uncontroversial (Write tool instead of
  `cat >`/heredoc; split large bash), and mu's ~0 rate shows it's avoidable. A
  `PreToolUse(Bash)` hook that warns/blocks heredoc-file-writes + oversized commands is a
  natural one-at-a-time A/B (with self-breadcrumb for treatment capture).
- Caveats: `shell_file_write` is broad (catches `echo x > /tmp/marker`); `large_bash`
  threshold (1200) is a first guess — both need a quick FP audit before enforcement.

Behavior note: cc (this session) was using python-in-heredoc + `cat >`-heredoc for
throwaways; switched to Write/Edit + small plain commands (auditable + clobber-safe).

## Findings — round 4 (FP-audit of the heredoc/shell-write candidates, 2026-06-22)

Sampled real cc hits before trusting round 3:
- `shell_file_write` v1 was **FP-heavy** — matched `echo …; … 2>/dev/null` (producer + a
  LATER redirect). Tightened: a `>`/`>>` not preceded by a fd digit/`&`, target a real path,
  excluding `/dev/*` and `/tmp` scratch. Refined cc rate 23% — barely moved when excluding
  `/tmp`, so most are genuine **non-scratch** writes (the overwrite concern is real).
- `heredoc` (`<<`) is the clean, unambiguous lead (196 cmds; 44 with a code interpreter).
- `large_bash`: cc bash cmd length median 382 / p90 1037 / p99 2517 / max 6117; `>1200` = top
  ~7% of commands — defensible but a guessed threshold.
Trustworthy candidates: heredoc + code_in_heredoc + large_bash; shell_file_write secondary.

## Findings — round 5 (violation/context × outcome, 2026-06-22)

Outcome = operator-frustration markers (scans.py MARKERS) in operator-typed cc text.
First pass used PRESENCE (≥1 marker): every predicate ~2–3× lift, several 100%, frustration
25%→96% with context — **all a session-LENGTH confound**: presence rises 26%→100% with tool
count because long sessions have many operator messages and a flat ~0.7%/msg marker rate
makes ≥1 near-certain.

Re-ran **exposure-normalized** (markers per operator message):
- by session size: rate FLAT-to-declining (607/577/651/122 per 1k) while presence went
  26→100% → the gradient was pure exposure; big sessions aren't more frustrating per message.
- by max context: rate 324 / 1111 / 327 per 1k (<40k / 40-150k / >150k) — **non-monotonic →
  H4 (rot rises with depth) NOT supported once normalized.** (40-150k spike interesting but n=30.)
- predicate rate-lift: **heredoc 2.9× (SURVIVES)**; large_bash 1.0×, shell_file_write 0.7×,
  code_in_heredoc 0.7×, edit_loop 0.6× (collapse/reverse).

Takeaways:
1. PRESENCE is the wrong outcome metric — use per-message RATE + size control.
2. Naive lifts were almost all exposure confounds. **Only `heredoc` survives** (~2.9×
   elevated per-message frustration) → strongest real enforcement candidate so far.
3. H4-as-stated unsupported in normalized local data; revisit on .172 with power.

Caveats: small n (context bands n=23–32, size bands n=6–12) → heredoc 2.9× + the 40-150k
spike need .172 power; only exposure controlled (degradation.py's multivariate regression is
the proper estimate); marker FPs inflate the base rate (`stop`/`no.` over-fire); per-message
count is rough.

## Findings — round 6 (windowed / temporal, 2026-06-22)

Sessions have WINDOWS, not one good/bad label (operator: good open → rework+frustration
arc → exhaustion-leave ~4-5am → gap → recovery-probe ~10am; good sessions also left
overnight, so the gap alone isn't the signal — the surrounding behavior is). Per-session
scalars (round 5) wash this out. `scripts/windowed.py`: per-message timestamps (present on
all cc sessions), STEER-vs-REWORK marker split, within-session trajectory, leave/return gap
signature.

Local result (23 cc sessions ≥6 msgs — small + short):
- REWORK 1st-half 0.067 / 2nd-half 0.045 (rose 4/23) — **no late-degradation arc locally**.
- STEER 0.235 (≫ rework), flat — operator language is mostly normal directive **steering**,
  not frustration; round-5 markers conflated them.
- gaps: 16; leave-hour mode **8am** (not the 4-5am of canonical bad days); rework/steer
  before-gap 43%; **recovery-probe after-gap 0%**.
- **Conclusion:** the windowing MECHANISM works, but the phenomenon (degradation arcs, 4am
  leaves, recovery probes) **isn't in the local corpus — it's on .172** (long day-sessions).

## Convergence (end of local prototyping, 2026-06-22)
Three independent rounds all point the same way: **the local jail subset is too small/short/
recent to test the real phenomena** (context-rot, degradation arcs, outcome correlation).
What IS done and validated locally: the toolkit (`adherence_probe`/`violations`/`outcome`/
`windowed`), the methodology (FP-audit; exposure normalization; steering-vs-rework split;
windowing), and the structural facts (mu ~5k vs cc ~27k initial; heredoc the one
normalization-surviving signal). The powered, length-controlled, windowed run belongs on
**.172** via `features.py`/`degradation.py` + the windowed probe over `~/ai-sessions`.

## Findings — round 7 (powered, on the TYPED `ev` layer, 2026-06-23)

**Method reset (the load-bearing correction).** Rounds 1–6 hand-globbed raw jsonl
(`adherence_probe.py`). That is the wrong layer, and terrain proved it: the cc
archive's first line is a session-init header (no `message.usage`), so the probe
ate a `None` and crashed; a mu `supervisor.jsonl` first line isn't even JSON. The
deployed pipeline already solves this — it unifies both fleets into the typed
mu-core `SessionEvent` stream: **cc** via `cc_telemetry.py` (typed parse through
the `mu_anthropic_py` pyo3 wheel → `cc_events_out/claude-code/*.jsonl`), **mu**
native, both registered in `engine.py`'s DuckDB **`ev`** view. All adherence
signals now compute over `ev` (or `cc_telemetry`/`mu-bridge` typed events), never
hand-parsed dicts. `adherence_probe.py` is retained as the round-1 record but
**deprecated**; its `ev` replacement is `scripts/context_disparity.py`.

**Hypothesis.** The structural mu↔cc initial-context disparity (round 1: mu ~5k
vs cc ~27k, on 92/358 local sessions) replicates at power on the full corpus when
measured over the typed `ev` view.

**Results** (`scripts/context_disparity.py`, `ev` on threadripper; per-turn ctx =
cc `assistant_message_event.message.usage` [input+cache_read+cache_creation], mu
`context_assembly.token_count_estimate`):

| fleet | sessions (≥2 ctx turns) | initial (med / p90) | max (med / p90 / max) | growth |
|---|---|---|---|---|
| cc (typed ev) | 735 | **25,999** / 37,249 | 68,924 / 607,455 / 999,071 | 3.24× |
| mu (typed ev) | 1,296 | **4,348** / 9,386 | 8,943 / 40,868 / 245,140 | 2.01× |

Corpus via `ev`: **cc 918 / mu 4,610** distinct sessions (vs local 92/358 — ~10×/13×).

**Conclusions.**
1. **Disparity confirmed and slightly wider: ~6×** (4,348 vs 25,999) at 10–13× the
   sample. Structural, not a small-n artifact. The mu↔cc context-disparity thesis
   (H4 substrate) holds at power.
2. cc growth **1.62×→3.24×**: the local subset was bench-diluted (bench is flat
   ~1.0× growth); real cc sessions grow ~3×. Bench can't be path-excluded in `ev`
   (sessions are keyed by UUID post-conversion), but bench would only pull cc
   *down* toward its ~19k/1.0× floor — so the disparity is, if anything, understated.
3. **Robust compaction is now in reach:** mu emits explicit `compaction_assembly`
   events (136) carrying `tokens_before`/`tokens_after` — round-1 plan item 1, no
   longer dependent on the unreliable cache-drop heuristic. cc shows no explicit
   compaction kind in `ev` (open: does cc_telemetry emit one, or is it absent?).

**Next iteration.**
- Graduate per-turn context-trajectory into `features.py` as real columns
  (`initial_ctx`, `max_ctx`, `growth`) over `ev`, so `degradation.py` permutation
  importance can formally test H4 (rot rises with depth) vs H5 (rule half-life) —
  the proper multivariate estimate the round-5 exposure-normalization only gestured at.
- Re-test the round-5 **heredoc 2.9× per-message frustration lift** and the
  non-monotonic H4 depth curve at power (exposure-normalized) on the now-powered corpus.
- Wire `compaction_assembly` as the compaction mark; resolve cc's compaction signal.
- Coordinate slices with the other active session on mu-dialogue (avoid duplication).

## Findings — round 8 (faux/test-provider audit on the mu side, 2026-06-23)

**Hypothesis.** mu's round-7 baseline is contaminated by `FauxProvider`
(`crates/mu-ai/src/faux.rs`, the echo/scripted test provider) — the mu analog of
cc bench — so excluding it shifts the mu numbers.

**Results.** Faux surfaces in the deployed data as **`model='faux'` = 824 mu
sessions** (≈18% of the 4,610), and every one is stamped
**`provider_kind='anthropic_api'`** — i.e. faux masquerades at the provider level,
inflating the `anthropic_api` session bucket ~7× (952 reported vs ~128 real). But
faux sessions emit **no `context_assembly` events** (echo/scripted never runs the
real assembly pipeline), so **0** of them are among the 1,296 context-bearing mu
sessions. mu disparity stats are **unchanged** after exclusion: initial med 4,348
/ p90 9,386, max med 8,943, growth 2.01×.

**Conclusions.**
1. The context-trajectory metric is **structurally immune** to faux. The ~6×
   mu↔cc disparity is now confound-checked on both sides: cc bench can't dilute it
   upward (round 7), mu faux doesn't enter it at all (round 8). The disparity holds.
2. Faux **does** heavily skew any `task_telemetry`-derived metric — 18% of mu
   sessions, ~7× `anthropic_api` inflation. **Any** future feature over token
   totals / cost / provider mix / raw session counts MUST filter `model='faux'`
   (and `provider_kind in ('faux','mock')`). The round-7 "mu 4,610 sessions"
   headcount itself carries ~824 faux → real mu ≈ 3,786.
3. Baked the faux exclusion into `context_disparity.py` (`FAUX_MU`) — a no-op for
   the disparity metric, but the canonical "real mu sessions" predicate to reuse.

**Next.** Graduate per-turn context-trajectory into `features.py` over `ev` with
the faux filter (+ a cc bench filter) as a shared "real sessions" predicate, then
run `degradation.py` permutation importance (H4 vs H5) and the powered,
exposure-normalized heredoc-2.9× / depth-curve re-test.

## Findings — round 9 (corpus maturity / timeline scoping, 2026-06-23)

The mu corpus is mu's whole logged life — only **2026-05 → 2026-06** — so
feature-presence is time-gated and must not be read as behavior. Per first-session
month:

| month | mu sessions | with ctx_assembly | with compaction | faux |
|---|---|---|---|---|
| 2026-05 | 1,708 | 1,635 (96%) | 0 | 744 |
| 2026-06 | 2,903 | 2,743 (94%) | 32 | 80 |

- **context_assembly is present throughout** (≥94% both months) — it predates the
  logged corpus, so the disparity metric (rounds 7–8) needs **no** maturity
  date-gate. The ~6× result stands unscoped.
- **Compaction is a June feature** (May 0 → June 32 sessions). It's recent and
  rarely triggered; **scope all compaction analysis to June+ and treat n≈32 as
  small**. May's zero is feature-absence, not "mu chose not to compact."
- **faux is development-era-heavy** (May 744 → June 80) — consistent with May being
  mu's heavy build/test phase. Already filtered from task_telemetry metrics (round 8).

The only maturity gate that bites is compaction (June+, small-n); context-disparity
is feature-stable across the corpus.

## Findings — round 10 (powered round-5 outcome re-test on the typed `ev` layer, 2026-06-23)

`scripts/outcome_powered.py` (ev replacement for the raw-jsonl `outcome.py`; reuses
`outcome.NEG` markers + `violations.violations()` verbatim). cc sessions with
operator msgs + tools: **653**; overall marker rate **94.6 / 1k operator msgs**.

**Hypothesis.** The round-5 local outcome findings replicate at power: (a) presence
is an exposure artifact, rate is the honest metric; (b) H4 (rot-with-depth)
non-monotonic; (c) heredoc carries a ~2.9× per-message frustration lift.

**Results.**
- by session size (n tools): presence 13→34→72→82% (confounded); **rate
  134→207→93→79 /1k** — non-monotonic, declines for large sessions.
- by max context (H4): presence 6→25→75%; **rate 75 → 282 → 84 /1k** (<40k /
  40–150k / >150k) — a sharp **mid-context (40–150k) peak**, n=223, low at both ends.
- predicate rate-lift (markers/msg with vs without): **heredoc 0.9×**,
  code_in_heredoc 0.8×, shell_file_write 0.6×, large_bash 0.5×, edit_loop 0.9× —
  **all ≤1.0×**.

**Conclusions.**
1. Exposure normalization **confirmed at power**: presence tracks length/depth;
   rate does not rise monotonically. Round-5 method validated on 653 sessions.
2. **H4 (monotonic rot with depth) refuted** — rate is non-monotonic. But the
   round-5 40–150k spike **replicates and is now powered** (282/1k, n=223 vs the
   old n=30): operator frustration peaks at *mid* context, not deep context. The
   >150k drop is likely session-character (long autonomous runs have sparse, less
   frustrated operator messages) — needs disaggregation, not yet causal.
3. **The heredoc 2.9× lift does NOT replicate (0.9× at power, n=187).** Round-5's
   "heredoc is the one surviving signal → strongest enforcement candidate" was a
   small-n artifact. No tool-stream predicate predicts frustration at power (all
   ≤1.0×); they track tool-heavy/autonomous session character. The auditability /
   overwrite-safety case for a heredoc guard (round 3) stands on its own merits,
   but the *outcome* correlation round 5 leaned on is gone.

**Next.** Disaggregate the mid-context (40–150k) peak — is it rework-grind, and
does the >150k drop coincide with autonomous/sparse-operator sessions or with
compaction? Then `degradation.py` permutation importance as the proper
multivariate estimate (does *any* feature predict the frustration/sentiment label
once length, fleet, model are controlled?).

Caveat: cc bench can't be path-excluded in `ev` (UUID-keyed post-conversion), but
bench is scripted (≈0 operator markers) so it dilutes rates toward zero, not up;
NEG markers carry FPs (`stop`/`no.`) that add roughly constant noise across bands,
so the relative (non-monotonic) shape survives but absolute rates are soft.

## Findings — round 11 (disaggregating the mid-context peak, 2026-06-23)

Broke the round-10 max-context bands down by operator density + marker concentration:

| band | n | msgs/s | tools/s | msgs/tool | rate/1k | top5-mkr-share | sess-w-markers |
|---|---|---|---|---|---|---|---|
| <40k | 232 | 1 | 4 | 0.249 | 74.6 | 50% | 6% |
| 40–150k | 223 | 1 | 15 | 0.100 | 282.2 | 24% | 26% |
| >150k | 198 | 29 | 168 | 0.168 | 83.9 | 19% | 76% |

**The round-10 ">150k = sparse autonomous" guess was wrong.** >150k sessions are
the opposite — high engagement (median 29 operator msgs, 168 tools) — and **76%
contain a frustration marker**. Their low per-message *rate* is a **dilution
artifact**: many operator messages spread the markers thin.

**Conclusions.**
1. **Presence rises monotonically with context** (6→26→76%) = exposure. **Rate
   peaks mid-context** (40–150k) and that peak is **broad-based** (top-5 sessions
   only 24% of the band's markers, 26% of sessions affected) — a real signal, not
   outliers: terse-instruction + heavy-work + dissatisfied.
2. The per-message rate is **itself confounded by operator-message density**, which
   varies systematically by band (msgs/tool 0.25 / 0.10 / 0.17) — a deeper confound
   than round 5's exposure point. Neither presence nor rate alone is a clean outcome.
3. **Univariate banding has reached its limit.** The honest estimate needs
   `degradation.py`'s multivariate model (control length, operator-msg count, fleet,
   model simultaneously; ask whether context-depth has *independent* signal on a
   proper sentiment label). That is round 12.

## Findings — round 12 (multivariate baseline at power — `degradation.py`, 2026-06-23)

Ran the proper multivariate estimate (`degradation.py`: HistGradientBoosting
predicts SIGNED operator-sentiment net=(pos−neg)/100msg from telemetry; 5-fold OOF,
permutation importance) on the deployed pipeline at power.

**Results.** Coverage: telemetry **3,635** sessions, **481** sentiment-labeled,
**457 joined** (3,178 unattended/no operator language). **Out-of-fold R² = −0.095**
(worse than the mean), MAE 30.5 net/100msg. Top permutation features (wall_p95,
input_tok, output_tok, cost_usd…) are **not interpretable under negative R²**.

**Conclusions.**
1. **Objective telemetry does NOT predict operator sentiment at power** (no OOF
   skill). The univariate frustration signals (rounds 5/10/11) do **not** cohere
   into an objective multivariate signature with the current feature set.
2. **But `max_ctx`/context-depth is NOT in `features.NUMERIC`** (calls, tokens,
   cost, wall, gaps, tool_calls, provider/model/fleet). So this is *not yet* an H4
   test — it's "do the existing features predict sentiment," and they don't.
3. The study's shape is now clear: the **robust** result is structural — the mu↔cc
   context disparity (rounds 7–9); the **telemetry→outcome predictive link is
   weak-to-absent** (rounds 10–12).

**Next (round 13).** Graduate per-turn context-depth into `features.py` over `ev`
(unified: mu `context_assembly.token_count_estimate`, cc `assistant_message_event`
usage), re-run `degradation.py`; if R² stays ≤0 and `max_ctx` doesn't carry it, H4
is **multivariate-refuted** — consistent with the univariate non-monotonicity.

## Findings — round 13 (graduate context-depth → H4 multivariate test, 2026-06-23)

Graduated per-turn context-depth into `features.py` (`ctx` CTE + `max_ctx` /
`init_ctx` in `NUMERIC`; unified ev extraction: mu `context_assembly`, cc
assistant-turn usage). Smoke OK (cc sample `max_ctx`=999,071). Re-ran
`degradation.py`.

**Results.** Same coverage (457 joined). **R² = −0.079** (was −0.095 without
context — both negative, no OOF skill). `max_ctx` ranks 5th in permutation
importance (0.089) but that is **uninterpretable under negative R²**.

**Conclusions.**
1. **H4 multivariate-refuted at power.** Adding context-depth does not give the
   model predictive skill on operator sentiment; its apparent importance is noise
   under a no-skill model. Consistent with the univariate non-monotonicity
   (rounds 10–11).
2. **No telemetry signature predicts operator sentiment** — even with context-depth.
   The outcome (operator frustration/sentiment) is **not telemetry-predictable** at
   power with this feature set.
3. **Strategic consequence:** enforcement testing should use the **deterministic
   mechanism check** (does a blocking hook drive its violation predicate → 0?), NOT
   outcome-prediction (no signal) — this overturns round 5's outcome-lift framing
   for picking enforcement candidates (heredoc).

Process note: `features.py` (a deployed shared file) was modified **in-workspace
only** and exercised via a shadow copy on the host; **not pushed/deployed**.
Deploying the `max_ctx` graduation is a separate sign-off step.

## Synthesis (rounds 7–13, powered on .172)
- **Robust positive:** the structural mu↔cc initial-context disparity (~6×: mu ~4.3k
  vs cc ~26k) — the substrate for "minimal context adheres better" — holds at power,
  confound-checked against cc bench, mu faux, and the maturity window.
- **Robust negative:** no objective telemetry signature (including context-depth)
  predicts operator sentiment at power. The univariate "outcome" signals are
  exposure/operator-density confounds or small-n artifacts; the round-5 heredoc 2.9×
  enforcement candidate collapses (0.9×); H4 is refuted univariately *and*
  multivariately.
- **Direction:** measure the *cause* (context disparity — done) and evaluate
  enforcement by *mechanism* (predicate→0 under a hook), not by an outcome model
  the data won't support.

## Caveats
- Benchmarks (`bench` in path) are excluded from both scripts by default.
- mu `model='faux'` (FauxProvider test runs) must be excluded from any
  task_telemetry-derived metric (round 8); it does not affect context_assembly.
- Local subset only (bench-heavy on cc); **not the real baseline** — that's the
  full ~/ai-sessions corpus on .172.
- Cross-fleet absolute token comparison is rough (different accounting).
- No outcome joined yet (marks/sentiment/violations) — round 1 is substrate only.

## Plan / next
1. Robust compaction = explicit events (+ investigate cc file-split); add mu's
   `compaction*` events.
2. Violation classifiers from the harvested hook predicates (deterministic ones:
   edit-before-read, force-push, commit-without-test, edit-loop, delete-tests,
   cruft-at-stop). Overlay on the context trajectory.
3. **Directive-timeline tiering** (treatment indicator): join session start-time
   to the jj history of ~/.claude & ~/.config/mu AGENTS.md/CLAUDE.md → bucket each
   session none / doc-only; self-breadcrumb every future hook → hook tier. Outcome
   = quality (marks/sentiment), violation = mechanism check (→0 under a blocking
   hook). The none→doc rung is runnable retroactively NOW.
4. Graduate context-depth features into `features.py` → `degradation.py`
   permutation importance to formally test H4 vs H5; `anomaly_worklist.py` to
   grow marks on weird sessions.
5. Run the whole thing on .172 over the real corpus.

## Provenance
Probe: `scripts/adherence_probe.py`. Conversation 2026-06-22 (cc session). Hook
predicate harvest: claude-code-guide agent (official docs + community repos).
