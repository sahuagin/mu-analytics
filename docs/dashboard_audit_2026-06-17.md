# Dashboard analytics audit — 2026-06-17

Source map: operator notes in `/home/tcovert/dashboard_qa.md`, current `main@origin` after PR #22, `proto/index.html`, `sample_data.py`, `panels.py`, and a live `./run sample_data.py` contract snapshot.

This is an instrument-panel audit, not a final design. It separates: (1) what the dashboard currently claims, (2) what is actually wired, (3) what is misleading or low-value, and (4) a practical PR sequence.


## Executive recommendation

Do the next work in two layers:

1. **Immediate UI honesty / noise cleanup** — low-risk, small PR. Stop the dashboard from implying precision it does not have: relabel `FOCUS`, hide or caveat artifact classifier panels, filter `faux` noise by default, make truncated lists say they are truncated, and rename `top session` to `highest-cost session`.
2. **Session identity / link-through** — first durable architecture PR. Make Sessions the review hub and ensure every interesting row elsewhere can open or search the same session. This unlocks Cost, Behavioral, Delegations, and future review workflows.

If choosing only one next PR, pick **Session identity / link-through** if you want compounding value; pick **UI honesty / noise cleanup** if you want the current live dashboard to stop misleading immediately.

The highest-value invariant for future dashboard work:

> Any panel that identifies a notable session, anomaly, cost, audit finding, worker, or rating must show time, explain why it is notable, and link to the review surface.

## Cross-cutting findings

### 1. The fleet selector is a high-confusion control

Current behavior: `STATE.fleet` only dims non-selected rows/charts via `emph()` / `rowDim()`. It does not filter counts, KPIs, trends, cost totals, or page-level claims.

Operator experience: it looks like a filter (`both / mu / claude-code`) but many numbers do not change. This makes every page feel suspect.

Decision needed:

- Either make it a real global filter, recomputing visible page data from filtered rows where possible; or
- Relabel it honestly as `highlight:` / `emphasis:` and keep totals global.

Recommendation: make it a real filter for row-derived pages (Overview, Sessions, Cost by model/fleet/kind, Behavioral tables) and use an explicit caveat where a panel is inherently mu-only.

### 2. Display IDs are not joinable across dashboard surfaces

There are at least three ID schemes:

- Sessions page: `sample_data._short_id(fleet, task_id)`, currently 32-bit blake2s display IDs like `mu·28466dec`.
- Flagged queue / per-ask: `"mu·" + daemon[:4]`, e.g. `mu·09f6`.
- Degradation/audit rows: canonical refs such as `mu:<daemon>:<session>` or shortened refs, rendered as inert text.

Result: a flagged row like `mu·b533` cannot be found in Sessions search, because it is not the Sessions display ID. This is the highest actionability break: the dashboard points at a problem but provides no route to inspect/mark it.

Recommendation: introduce a small session identity map in the DATA contract:

```js
session_index: {
  by_display_id: {...},
  by_ref: {"mu:<daemon>:<sid>": "mu·28466dec", ...},
  by_daemon_prefix: {"09f6": ["mu·..."]}
}
```

Then make flagged, audit, sentiment-probe, top-cost, and delegation rows carry `session_id` / `display_id` where possible. Link rows to `#/sessions?open=<id>` or equivalent.

### 3. Faux/test sessions dominate default views

Live contract snapshot:

- `all_sessions`: 2,839
- `cost_by_kind.free`: 1,858 sessions
- newest day includes many `model: faux`, zero-tool, zero-cost sessions.

Operator intent: faux providers are test noise and should be filtered by default.

Recommendation: add a dashboard data policy:

- default visible rows exclude `model == "faux"` and probably zero-tool/zero-cost synthetic test sessions;
- include a `show test/free noise` toggle for diagnostics;
- cost totals should report whether test/free rows are excluded.

### 4. Outcome taxonomy is currently not useful enough for prominent panels

Live outcomes:

```json
[
  {"outcome":"narrative_no_action", "sessions":2815},
  {"outcome":"error_exit", "sessions":23},
  {"outcome":"operator_intervention", "sessions":1}
]
```

This makes the prominent Session outcomes panels mostly an artifact display. `hallucination_by_model` is derived from the old `HALLU = {narrative_no_action, hollow_commit, lying_state}` set over tool-using sessions, so it currently reports ~100% hallucination for nearly every real model. That is not trustworthy.

Recommendation: demote or hide outcome/hallucination panels until the enricher/taxonomy is repaired. Replace with event-log signals that are currently meaningful: degraded stop reasons, error exits, tool error loops, callouts, audit findings, manual marks, and sentiment residuals.

### 5. Tables that identify interesting sessions are mostly inert

Flagged queue, audit findings, sentiment residuals, top-cost session, per-ask session selector, and delegations all expose something worth investigating, but most are not clickable and many do not show date/recency.

Recommendation: actionability rule for dashboard rows:

> If a row represents a session, worker, audit finding, or anomaly, it should show time and link to the closest review surface.

For v1 this can simply open/filter the Sessions page; later it can deep-link a transcript/event detail.

## Page-by-page audit

## Overview

Current useful parts:

- top-level billed vs subscription vs total API-rate-equivalent spend;
- sessions count by fleet;
- cost by fleet/model;
- cost/degradation trend, if the degradation signal is understood as stop-reason based.

Problems:

1. `Free $0.00` occupies prime space but provides little value. It currently mostly means self-hosted/test/faux/zero-cost sessions, not an avoided-cost story.
2. Subscription is API-rate-equivalent only, not actual subscription spend. Operator wants subscription windows and costs so the dashboard can compare actual subscription cost/token vs API-equivalent cost.
3. Focus selector only dims. Top KPIs do not recompute.
4. Session outcomes panel is dominated by `narrative_no_action` and is not an operational signal.
5. Faux sessions should be filtered by default.

Recommended changes:

- Replace `Free $0` with either:
  - `Avoided API cost` for self-hosted/free providers, excluding faux; or
  - remove/shrink it into a secondary cost-kind table.
- Add `subscriptions.toml` or `[subscriptions]` in config with provider/account, start/end, monthly cost, and plan label. Use it to show actual subscription spend alongside API-rate-equivalent.
- Remove Session outcomes from Overview until taxonomy is useful, or replace with `Attention queue` summary: degraded finishes, error exits, audit findings, unreviewed high-cost sessions.
- Default filter out `faux`/test rows.

Priority: high for faux filtering and outcome demotion; medium for subscription actual-cost modeling.

## Sessions

Current useful parts:

- It is the most valuable page: grouped by day, model/outcome filters, expandable transcript sidecars, local marking/export.
- Live data has full sidecar drill-downs for thousands of sessions.

Problems:

1. Search only checks `(s.id + " " + s.model)`. It does not search outcome, provider/kind, raw canonical refs, daemon/session, notes, or alternate IDs from other panels.
2. It cannot find flagged IDs because flagged IDs are daemon-prefix IDs, not Sessions display IDs.
3. Defaults include faux/test sessions, polluting newest-day view.
4. Marking is local/export-based and not visibly reconciled with `marks_store` except after manual ingest/refresh. This is okay for now, but the UI should state the lifecycle.
5. `$ / call` divides by `tool_calls`; zero-tool sessions can produce misleading values.

Recommended changes:

- Add canonical session identity fields to each session row: `ref`, maybe `daemon`, `sid`, `task_id` when safe; search across all of them.
- Support URL state: `#/sessions?open=<display_id>` and `?q=<query>`.
- Filter faux/test sessions by default with a visible toggle.
- Add a small row/date summary: newest/oldest time if available, not just day.
- Improve zero-tool handling for `$ / call`.

Priority: highest. This is the hub that makes every other page actionable.

## Cost & Cache

Current useful parts:

- Cost and cache are genuinely valuable; operator explicitly likes this area.
- Cache economics uses config multipliers and event-derived median/p90 inter-ask gaps.
- Per-ask session bars exist, but sample frame is unclear.

Problems:

1. `Top session` means top-cost session, but UI does not say that. The $866.29-style number makes the operator want to inspect it, but there is no link.
2. `cost_composition_top_session` is computed from the top-cost row, but the front-end does not display the session ID/date/model next to the composition.
3. `per_ask_sessions` is mu-only in practice and uses daemon-prefix IDs; it does not link to Sessions and lacks date.
4. It samples 12 sessions from a backend candidate set selected by Claude-like model/cache-write visibility, not all sessions. The UI says “sampled sessions” but not enough.
5. The 5m-cache expiration / near-miss question is not directly represented. Current `cache_econ` gives corpus median gap 0.22m and p90 1.96m, which hides operator-relevant long-prompt/screen-switch misses and cc 3–5+ minute requests.
6. `Cost by kind` and `Cost composition` take more space than their current utility.
7. `rate-equiv` term needs a glossary/tooltip.

Recommended changes:

- Rename `top session` -> `highest-cost session`; include ID/model/date and a Sessions link.
- Add a cache-expiry panel:
  - count asks with previous gap >5m and <=60m;
  - count near misses, e.g. 4–6m;
  - split interactive vs `mu ask` if detectable;
  - separately track request duration that crosses 5m for cc if timestamps permit.
- Add date/model/fleet to `per_ask_sessions`; carry real display ID; make dropdown searchable or include a “show highest cost / show longest ask gap / show cache misses” selector.
- Shrink cost-kind visuals; keep caveat but reduce footprint.

Priority: high after Sessions identity work. Cache miss analysis is a high-value second PR.

## Behavioral

Current useful parts:

- This is the right conceptual page: degradation, honesty, review queue, manual ratings, audit, sentiment residuals.
- `Telemetry → operator-sentiment probe` is promising.
- `mu-audit process findings` has real data from refresh.

Problems:

1. Enrichment is still pending; the page says so, but then still gives large space to artifacts of the broken classifier.
2. Session outcomes duplicate Overview and are dominated by `narrative_no_action`.
3. Degradation over time uses stop-reason degradation line plus manual marks, but marks are sparse and plotted at day resolution. Live `marks_n` is 10, operator has marked more historically/locally than the current plotted dots suggest.
4. Hallucination rate is not credible: live output is ~1.0 for most models because it treats `narrative_no_action` as hallucination. This should be hidden/demoted now.
5. Flagged queue shows 12 because backend hard-limits `flagged_queue(con, limit=12)`. UI does not say it is a top/sample of a larger queue. Rows are not clickable and carry non-joinable IDs.
6. Audit findings show 12 of 16 (`A.slice(0,12)`), while header says 16. No click-through or time clarity.
7. The relationship between Flagged, Audit, ML residuals, manual marks, and stop-reason degradation is not explained.

Recommended changes:

- Replace Session outcomes + Hallucination rate with an `Attention queue` layout:
  - degraded stop reasons;
  - error/tool-error loops;
  - audit findings;
  - telemetry/operator residual tails;
  - manual low ratings.
- Make every row linkable to Sessions.
- Show `showing 12 of N` and add expand/all toggles for flagged/audit/residual rows.
- Add filters/toggles for manual ratings vs machine/audit signals in the trend.
- Add time resolution: day view first, later zoom/hour buckets for time-of-day theories.
- Define terms on page: “degradation line = non-clean stop_reason share”, “rating stars = operator marks”, “residual = predicted sentiment - observed sentiment”.

Priority: high, but depends on Sessions identity/linking.

## Internal Ops

Current useful parts:

- Tool mix is valuable, especially if both mu and cc are included and normalized.
- Context trajectory and compaction are useful for diagnosing mu compaction.
- Recall provenance may be useful but needs a clearer question.

Problems:

1. Page is labeled mu-only, but the top fleet selector still appears. Operator correctly notes some cc-derived information can exist.
2. Tool mix already appears to include both casing families (`Bash`/`bash`, `Read`/`read`), suggesting mixed fleet/tool naming. It needs normalization and fleet split.
3. Recall provenance panel is large for unclear value: live data only two buckets (`ProjectFile`, `Memory`) with item/token counts.
4. Context trajectory is one selected demo daemon. Backend `_demo_daemon()` picks a single session with a visible sawtooth. There is no selector or comparison.
5. Context trajectory y-axis should become logarithmic or selectable when comparing mu/cc or different context scales.
6. cc compaction synthesis is absent but plausible: detect `/compact`/`/clear`/summary events and context-size drops from cc transcripts/events.

Recommended changes:

- Split Internal Ops into:
  - `Tooling`: normalized tool mix, by fleet/model/date, t4c coverage hints.
  - `Context & compaction`: mu real compaction now, cc synthesized compaction later.
  - `Recall`: smaller, or moved behind details until it answers a concrete question.
- Normalize tool names (`Bash` vs `bash`, `Read` vs `read`) and include fleet dimension.
- Add session selector for context trajectory; later allow comparing 2–4 selected sessions.
- Plan cc compaction synthesis as a separate data PR.

Priority: medium. Tool normalization is a good small win; cc compaction is larger.

## Delegations

Current useful parts:

- Newly wired; real data exists: live snapshot 26 workers, 142-ish mailbox events.
- Model/outcome filters are already present.

Problems:

1. KPI cards lack recency. Operator cannot tell whether the mailbox/workers are from recent work or tests weeks ago.
2. Rows include started timestamp in data, but UI table does not display it.
3. Session refs are canonical-ish but not linked to Sessions.
4. “running” may be stale if terminal events were not paired or are missing; the page should distinguish live running vs no terminal event recorded in historical data.

Recommended changes:

- Add first/last delegation time and last 24h/7d counts.
- Display started date/time in worker rows.
- Link worker rows to orchestrator session detail where possible.
- Add “stale running?” caveat if started is older than a threshold.

Priority: medium-small. Good follow-up after identity/linking.

## Suggested PR sequence

### PR 1: Dashboard actionability and noise cleanup foundation

Goal: make the existing dashboard less misleading without changing the data model deeply.

Scope:

1. Filter faux/test rows by default in `sample_data._build_sink()` or front-end, with a visible toggle if front-end.
2. Rename/relabel fleet control to `highlight` if not implementing real filtering yet.
3. Hide/demote Hallucination rate and duplicate Session outcomes where they are artifact-dominated; replace with explanatory caveats or compact stop-reason/audit summaries.
4. Make visible counts honest: `showing 12 of N` for flagged/audit if backend returns total or front-end slices.
5. Rename `top session` -> `highest-cost session`; include ID/model/date if available.
6. Add small glossary text for `rate-equiv` and `notional subscription`.

Why first: it immediately reduces false confidence and screen noise while preserving current architecture.

Risk: low. Mostly UI/contract cleanup.

### PR 2: Session identity/link-through

Goal: make every interesting row navigable to review.

Scope:

1. Add canonical session refs/display IDs to session rows.
2. Change flagged/per-ask/audit/degradation/delegation outputs to carry display IDs where possible.
3. Add Sessions URL state (`open`, `q`) and make rows link to it.
4. Expand Sessions search across display ID, canonical ref, daemon/session/task IDs, model, outcome.

Why second: this unlocks the rest of the dashboard. Without it, every anomaly panel is a cul-de-sac.

Risk: medium. Needs care to keep `sample_data._short_id()` and `panels.write_session_transcripts()` keys aligned.

### PR 3: Cost/cache improvements

Goal: answer the operator's real cache questions.

Scope:

1. Add 5m expiry / near-miss analysis to `panels.cache_econ()`.
2. Add date/model/display ID to per-ask sessions.
3. Link highest-cost/per-ask sessions to Sessions.
4. Shrink cost-kind/composition panels.

Risk: medium-low.

### PR 4: Behavioral review workbench

Goal: turn Behavioral into a review queue, not a classifier artifact gallery.

Scope:

1. Unified attention queue combining flagged, audit, ML residual, manual low ratings.
2. Click-through to Sessions.
3. Trend toggles: stop-reason degradation, manual marks, audit/ML candidates.
4. `show all` / filters by reason, model, fleet, time.

Risk: medium.

### PR 5: Internal ops deepening

Goal: make internal operational signals comparable and useful.

Scope:

1. Normalize tool names and split by fleet.
2. Add context trajectory selector.
3. Compress or clarify recall provenance.
4. Design/implement cc compaction synthesis separately.

Risk: medium-high for cc compaction; low for tool normalization.

## Immediate recommended first PR

If we want the smallest useful code PR next, I recommend:

**“dashboard: reduce misleading controls and artifact panels”**

- relabel `FOCUS` to `HIGHLIGHT` in the shell copy;
- rename `top session` labels to `highest-cost session`;
- add top-session ID/model/date to the cost composition header if already available in `top_sessions[0]`;
- hide or caveat Hallucination rate as “disabled: classifier artifact” when `narrative_no_action` dominates outcomes;
- change audit/flagged labels to `showing 12 of N` where applicable;
- filter `model == "faux"` from default `all_sessions` / aggregates or add a visible toggle/caveat if filtering aggregates is too broad for one PR.

However, if we can tolerate a slightly more architectural PR, the better first durable step is **Session identity/link-through**, because it fixes the main actionability failure that appears across Cost, Behavioral, and Delegations.


## Open product decisions for discussion

These are the few places where code can move in more than one reasonable direction and operator preference should decide.

1. **Fleet selector semantics**: should `mu / claude-code / both` become a real filter everywhere possible, or remain a highlight control with honest labeling? Real filtering is more useful but requires each panel to declare what it can and cannot filter.
2. **Default corpus policy**: should default views exclude only `model == "faux"`, or also exclude all zero-tool/zero-cost sessions? The latter removes more noise but risks hiding legitimate free/local work.
3. **Subscription accounting**: should actual subscription cost be shown as calendar-month spend, rolling-window amortized spend, or both? This affects how `subscription` compares to API-rate-equivalent.
4. **Behavioral page identity**: should it remain a dashboard page, or become primarily a review queue/workbench with charts as supporting context? The audit recommends the latter.
5. **Marks lifecycle**: should local browser marks remain export/import based, or should dashboard marks post directly into a local endpoint/store? Direct write is smoother but changes the deployment/security shape.
6. **cc compaction synthesis**: should inferred cc compactions be labeled as synthesized events in the same contract as mu compactions, or kept visually separate until confidence is high?

## Candidate issue breakdown

If tracking this in beads/issues, these are clean slices:

- `dashboard-ui-honesty`: relabel focus/highlight, rename top/highest-cost, make truncation counts honest, hide/caveat artifact classifier panels.
- `dashboard-filter-test-noise`: default-exclude `faux` and decide zero-tool/zero-cost policy.
- `dashboard-session-identity`: add canonical refs/display IDs/session index and URL-open support.
- `dashboard-link-through`: make flagged/audit/probe/per-ask/delegation/top-cost rows open Sessions. Depends on session identity.
- `dashboard-cache-expiry`: add 5m expiry and near-miss analysis, with interactive vs ask split if detectable.
- `dashboard-subscription-accounting`: add subscription windows/cost config and actual-vs-rate-equivalent reporting.
- `dashboard-behavioral-workbench`: replace artifact outcome/hallucination panels with unified attention queue and trend toggles.
- `dashboard-tool-normalization`: normalize tool names and split tool mix by fleet.
- `dashboard-cc-compaction-synthesis`: infer cc clear/compact/summary events and visualize separately from native mu compactions.


## Anti-goals / avoid in first implementation pass

These changes are tempting but should not be first unless explicitly chosen:

- Do not tune the hallucination classifier superficially just to make the chart look plausible. The underlying taxonomy/enricher issue should be fixed or the panel should be demoted.
- Do not add more charts before making existing rows link to reviewable sessions. The actionability gap is larger than the visualization gap.
- Do not make `faux` disappear without leaving a diagnostic path to include test/free rows when needed.
- Do not merge inferred cc compaction with native mu compaction without visible provenance; synthesized signals should be labeled until validated.
- Do not treat subscription API-rate-equivalent as real spend. It is useful, but it must stay visibly distinct from actual subscription cost.
- Do not expand the page with another large static table when a searchable/linkable Sessions view would answer the question better.


## Tracking beads

Filed under the mu beads project (parent `mu-mucm`):

- `mu-mucm.7` — Dashboard analytics audit follow-up (epic)
- `mu-mucm.7.1` — Dashboard UI honesty and noise cleanup
- `mu-mucm.7.2` — Dashboard session identity and link-through
- `mu-mucm.7.3` — Dashboard cache expiry and near-miss analysis
- `mu-mucm.7.4` — Dashboard subscription accounting
- `mu-mucm.7.5` — Behavioral dashboard review workbench
- `mu-mucm.7.6` — Internal ops dashboard deepening

## Implementation hooks for the first two PRs

These are the concrete code touch-points I found while auditing, so the next agent does not have to rediscover them.

### UI honesty/noise cleanup hooks

- Fleet control label lives in `proto/index.html` around the shell header (`id="fleet"`) and is consumed only by `STATE.fleet`, `emph()`, and `rowDim()`.
- Overview panels are in `renderOverview(v)`.
- Cost page wording and `top session` labels are in `renderCost(v)`, especially the KPI `Cache-read share` and panel `Cost composition · top session`.
- Behavioral artifact panels are in `renderBehavioral(v)`:
  - `Session outcomes` calls `outBars('b-out', DATA.outcomes)`;
  - `Hallucination rate by model` calls `barH('b-hall', DATA.hallucination_by_model...)`.
- Audit display currently slices silently: `(DATA.audit_findings||[]).slice(0,12)`.
- Flagged queue count is already pre-limited by backend `panels.flagged_queue(con, limit=12)`, so honest `showing N of total` needs either a backend total or a larger returned list with frontend slicing.
- Default sessions data is built in `sample_data._build_sink()`. Faux rows enter through `_load()` / `_sessionize_mu()` and are currently included in all aggregations. If filtering backend-side, decide whether `faux` is excluded from only `all_sessions` or also aggregate costs/counts.

### Session identity/link-through hooks

- Display IDs are produced by `sample_data._short_id(fleet, task_id)`.
- Sessions rows are produced in `_build_sink()` under `top_sessions` and `all_sessions`.
- Transcript sidecars must stay aligned with display IDs:
  - `panels._TX_KEY` defines natural keys (`mu: daemon/session_id`, `cc: task_telemetry.task_id`);
  - `panels.write_session_transcripts()` writes sidecars using `_short_id(fleet, key)` and `_slug()`.
- Sessions table state lives in `proto/index.html` global `SS` and `sessionTable()`.
- Sessions search currently checks only `(s.id + ' ' + s.model).toLowerCase().includes(q)`.
- URL routing currently ignores query params: `route()` reads `location.hash.replace('#/','')` and finds a page by exact ID. `#/sessions?open=...` will need parsing.
- Flagged queue IDs are currently non-joinable daemon prefixes: `panels.flagged_queue()` returns `"mu·" + daemon[:4]`.
- Per-ask session IDs are also daemon prefixes in `panels.per_ask_sessions()`.
- Delegations rows carry `session_ref = f"{fleet}:{session}"`, but not the display ID / sidecar key.
- Audit and ML-probe rows carry refs from refresh-produced files; those should be mapped through the same session index where possible.

### Suggested acceptance criteria for PR 1

- The top selector no longer appears to be a filter unless it actually filters.
- Newest Sessions day is not visually dominated by `faux` rows by default.
- No panel prominently reports hallucination rate as if it were credible when `narrative_no_action` dominates outcomes.
- Cost page says `highest-cost session`, not `top session`, and shows enough identity to pursue it.
- Any truncated list says it is truncated.

### Suggested acceptance criteria for PR 2

- A flagged queue row can be clicked and opens the corresponding Sessions drill-down.
- Searching Sessions for a visible ID/ref from Flagged, Audit, Delegations, or Per-ask finds the same session.
- Existing transcript sidecars still load for opened rows.
- `just check` stays green.
