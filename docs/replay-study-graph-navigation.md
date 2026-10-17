# Replay study: does graph navigation beat grep for coding agents?

Design doc, 2026-10-16. Paired-arm comparison on real past tasks from this
fleet, at their original revisions, with future-leakage isolated.

## Why

The code_index graph tools (code_neighbors et al., #78/#80/#81) now produce
edges we trust (self-receiver veto live; ghosts gone; all five sources
edge-converged). The open question is whether an agent *using* them solves
navigation-heavy tasks better than one without — and whether they're found at
all when present but unprompted (activation-energy failure predicted by the
May LSP memory; confirmed for bake-week: 8/8 code_neighbors calls in event
logs were the author's own probes, zero organic usage).

## Tasks (pinned, gold-diff-backed)

All in the mu repo; gold = the merged historical change, arm state = its
parent revision.

- **T1 — context metadata resolution** (Aug 3). Session `100c058851c356a5`.
  Arm revision `e0e38968` (#506 merge), gold `33b0ede9` "fix direct-session
  context metadata resolution", 2 files (session.rs, route_catalog.rs),
  127+/21−. Classic branch-finding navigation: locate the per-model context
  display paths, find why 5.6-sol misses them.
- **T2 — config rejection / swallowed error** (Aug 13). Session
  `d835c2bfa918d8ba` (origin `258fee87b0ce912c`). Gold candidates pinned in
  the Aug window (`mu-tx6r solo: surface the daemon's CONFIG ERROR banner in
  the TUI`, `mu-y0dw loop: retry transient stream errors`); exact rev pair
  to be confirmed during staging — the *bug fix* may predate those surfacing
  commits; pick the pair whose diff actually changes rejection behavior.
- **T3 — io71s + roz1e pair** (Aug 27-28). Session `0fbb7ac8dd0cf6bb` —
  itself a replay experiment Thaddeus ran to test the 27k-loop fixes. Golds:
  `a8b5d1a7` (mu-io71s autonomy-gate) and `ea0cac85` (mu-roz1e push_back
  revert) on `kzuwzpnn`/`oromlsvk`. Two-part task; T3 runs later (largest).

Composite-session note: briefs are *synthesized* from the original task
intent (first real task message of the session), not copied from sessions
that contain meta-discussion. Cross-session task stitching is allowed where
the work spans sessions (T2).

## Isolation (the future-leak problem)

Live indexes are at *current* main — a replay at Aug-3 state must never query
them (symbols/line numbers from the future). Mechanism, verified in code:

1. **Per-replay DB**: `code-index --db /abs/replay-<tid>.db ingest <workspace>`
   + `graph build` at the arm revision. Embeddings via OPENROUTER_API_KEY
   (tq); ~minutes per task.
2. **GN arm queries its replay DB by absolute `db=` param** — MCP server
   resolves absolute paths (mcp.rs `resolve_db`).
3. **Live daemon stays stopped for the whole study** (172 supervisor
   `daemon: code_index_mcp` stopped 2026-10-16; child kills restart, the
   supervisor pid must die). Arms run their own `code-index-mcp` instance
   pointed at their replay DB; nothing else listens on 7622.
4. Post-run audit: event logs confirm every tool call's db arg == the arm's
   replay DB. Any arm that touched another DB is flagged, not silently mixed.

## Arms

- **G** — `mu ask` default (MCP disabled): read/glob/grep/bash/edit only.
- **GN** — MCP enabled, brief names the graph tools, `db=` stated in brief.
- **GN-free** (exploratory) — MCP enabled, tools present, brief says nothing.
  Answers activation energy directly: does the model find `code_neighbors`
  from the tool schema alone?

Same role both arms: `qwen3.8-flash-next` via flashnext provider (privacy
constraint from Thaddeus: only flash-next processes session content), same
thinking effort, same tool grants, fresh `jj new <parent>` workspace each.

## Brief template (identical text except the one added paragraph)

Task intent (from original session's first task message) + repo path +
"make the fix; tests must pass; commit" + [GN only: "You have code_neighbors
and code_recall MCP tools available against this repo; pass db=..."].

## Metrics

- calls-to-first-touch-of-a-gold-file (navigation efficiency)
- wrong-file visits before first gold touch
- gold-diff overlap: jj diff arm-vs-parent compared against gold diff
  (files touched; symbol overlap; do-not-touch violations)
- wall time, tool-call count, cost
- compile/test success at commit
- GN-free: did any code_neighbors call happen before/after first grep?

## Run plan

Pilot: T1 × {G, GN, GN-free} = 3 spawns, sequential (single box), 3600s cap
each, then review of the three logs. Scale to T2 (+T3 if pilot clean) only
after pilot review. Bake-week organic-usage mining continues alongside;
week-end reading is part of the report either way.

## Threats

- Single model, single repo, n=3 cells — directional, not significant.
- T1's gold was authored *by a session in the fleet* whose brief Thaddeus
  wrote; brief synthesis inherits that framing (fair: same shape both arms).
- Index freshness at parent revision = perfect by construction (built from
  the checked-out parent), but embeddings pull live API (cost, nondeterminism
  in recall ranking — GN arm only; acceptable).
- flash-next nondeterminism: one run per cell in pilot; if results look
  promising, rerun ×3 for variance.

## Deploy note

Live daemon restart procedure when the study ends: supervisor pattern is
`daemon -r` under /etc/rc.d; stop requires killing the *supervisor* pid
(child kills just respawn — learned the hard way).
