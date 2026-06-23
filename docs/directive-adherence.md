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

## Caveats
- Benchmarks (`bench` in path) are excluded from both scripts by default.
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
