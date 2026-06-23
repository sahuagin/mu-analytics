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

## Caveats
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
