# cache_sim — prefix-cache simulator over mu session traces

`cache_sim.py` replays each mu session's prompt rope from the event log, call by
call, and measures how many prompt tokens a serving side would have to prefill
under different cache protocols. It exists to answer one question with our own
traces before anyone writes server code: **how much does an interior edit
(compaction, skill activation) cost a prefix-only cache, and how much of that
would a segment-addressed cache recover?**

Design lineage: mu's rope/source-map architecture
(`mu/specs/architecture/event-sourced-context.md`), the operator's May-2026
pointer-set compaction model, and "Context Language Models" (arXiv 2609.37725)
whose Suffix Cache Reuse is the server-side half of the same idea.

## What the log does and does not hold

The simulator never reads a stored rope; mu's `context_assembly` event records
counts and a provenance breadcrumb, not the span list. The rope is **rebuilt** by
replaying the agent loop's message Vec and then **checked** against the snapshot.

Validated identities (probe session `6f4ccbbb7e7c79f6`, 1151 calls, 0 mismatches;
archive sweep, 36k calls, 1 mismatch):

| snapshot field | meaning |
|---|---|
| `message_count` | length of the **uncompacted** message Vec (seed + live events) |
| `span_count` | the **compacted** rope: live messages + summaries + `tool_count` + a constant prefix (3 spans in practice) |
| `token_count_estimate` | post-compaction renderer estimate (`== tokens_after` on a compaction call) |

Span ids follow `mu-core/src/context/assembly.rs`: `msg-{idx}-user`,
`msg-{idx}-assistant`, `msg-{idx}-tool-result:{call_id}`, where `idx` is the
position in the uncompacted Vec. Ids are stable across compaction (the baseline
rope keeps compacted spans; new messages append with their Vec offset), which is
what makes span identity across calls meaningful at all.

Replay inputs: `continuation_seeded.messages` (a resumed head's inherited history,
same `role`-tagged `AgentMessage` serde), then `user_message` /
`assistant_message_event` / `tool_result` in log order; `compaction_assembly.decisions`
remove dropped and absorbed ids and add the summary id; `context_cleared` resets.

Known coarse spots:

- The non-message prefix (system prompt, project files, memory recall, tool
  schemas) is itemized only as `first_span_ids` (5) + `tool_count` +
  `token_breakdown`. It is modelled as two pseudo-spans (static, tool-schemas).
  A one-tool change therefore recomputes the whole schema block under every
  model. In the archive `prefix_changes=0`, so this never fired.
- Per-span token weights are chars/4 rescaled per kind per call to the logged
  `token_breakdown`, so kind totals match the renderer's estimate exactly and
  the split between spans of one kind is proportional to length.
- Summary spans are placed before the surviving messages; their position is not
  logged. Moot today: every compaction in the archive is `span-family-drop`, which
  emits no summaries.

## Cache models

Per consecutive pair of model calls in a session, with spans keyed by
`(id, content-hash)`:

- **flat** — longest common prefix; everything after the first difference is
  re-prefilled. Upper bound for every prefix-only cache (Anthropic breakpoints,
  OpenAI automatic caching, vLLM/SGLang radix). Real caches do worse (block
  granularity, eviction, routing), never better.
- **seg k=K r=R** — flat, plus up to K relocated *runs*: contiguous stretches of
  survivors that were also contiguous in the previous prompt shift by one offset
  and count as one relocation (the paper's "chunk"; K=6 is its default). R is the
  residency rule: `prev` holds only the previous call's spans, `session` holds
  every span ever sent. Relocation is the approximate operation and is reported
  separately from exact prefix hits.

## Results — compacted sessions, 2026-10-04

63 sessions with at least one compaction, both archive roots on this host
(`~tcovert/.local/share/mu/events`, `~claude/.local/share/mu/events`), 36,324 model
calls, 4.07 B prompt tokens sent.

| slice | calls | flat prefill (% of sent) | seg k=6 prefill | savings vs flat |
|---|---|---|---|---|
| all calls | 36,324 | 85.8 M (2.1%) | 28.2 M (0.7%) | 67.1% |
| compaction calls | 993 | 62.0 M (67.6%) | 4.4 M (4.8%) | 92.9% |
| steady (append-only) calls | 35,331 | 23.8 M (0.6%) | 23.8 M (0.6%) | 0% |

Reading it:

- Append-only turns are already ~99.4% cached under a plain prefix cache. There
  is nothing for a segment protocol to win there.
- A compaction call re-prefills **two thirds of the prompt** under a prefix cache,
  because `span-family-drop` evicts old tool clusters from the *interior* and
  everything after the first gap moves.
- Six run relocations recover essentially all of it (K=6 ≈ K=∞). mu's heuristic
  leaves few gaps per compaction, so the chunk cap the paper worried about is not
  binding on this workload.
- `r=session` equals `r=prev` everywhere: nothing is ever re-inserted after
  eviction today, so there is no recalled span for a longer residency to catch.
  That column becomes meaningful only once an archive-and-recall controller exists.
- Per provider the savings track compaction frequency: ollama (small context,
  compacts every other call) 88%, vllm143 78%, openai_codex 42%, flashnext 26%.

### Full archive, same day

Every session with ≥2 model calls on both roots: 8,472 sessions, 108,607 calls,
6.22 B prompt tokens sent, 1 reconstruction mismatch.

| slice | calls | flat prefill (% of sent) | seg k=6 prefill | savings vs flat |
|---|---|---|---|---|
| all calls | 108,607 | 331.4 M (5.3%) | 273.5 M (4.4%) | 17.5% |
| compaction calls | 993 | 62.0 M (67.6%) | 4.4 M (4.8%) | 92.9% |
| steady calls | 107,614 | 269.4 M (4.4%) | 269.1 M (4.4%) | 0.1% |

The whole fleet-wide gain lives in the 993 compaction calls; steady-state prefill
is session starts (8,472 cold prefixes) plus each turn's genuinely new material,
which no protocol avoids. `prefix_changes=39` (25 of them ollama) are the
tool-schema/skill changes the coarse prefix model does see.

A cross-session effect the simulator does not model: `anthropic_api` reports
*more* cache reads than the flat model predicts (6 calls, ratio 24×) because
Anthropic's cache survives across sessions sharing a bootloader prefix, while the
simulator starts every session cold. The `r=session` residency rule is the
single-session version of that; a fleet-wide residency rule would be the next
refinement if hosted-provider accounting ever matters here.

## Validation against provider-reported cache reads

Where the assistant usage carries `cache_read_input_tokens`, the report compares
simulated flat uncached tokens against reported uncached input (honoring
`usage_semantics.cache_read_in_input`). Providers that never report a figure
(vllm143: 100% null) are excluded rather than counted as uncached.

| provider | calls (full archive) | sim flat uncached / reported uncached |
|---|---|---|
| flashnext | 9,203 | 0.74 |
| openrouter | 29,102 | 0.28 |
| openai_api | 509 | 0.11 |
| openai_codex | 18,957 | **0.056** |

0.6–0.75 is the expected shape: the ideal prefix model under-predicts real
re-prefill by a quarter to a third (block granularity, tokenizer mismatch vs the
chars/4 estimate, hybrid-attention models snapshotting recurrent state).
OpenRouter at 0.28 is routing across backends with their own caches. The codex
figure is not explainable that way:
the ideal model predicts 20× fewer uncached tokens than OpenAI reports, and the
per-call `cache_read_input_tokens` sequence is erratic (68k, 53k, 10k, 52k, 2k, 7k,
0, 7k, 0, 100k on consecutive ~100k-token prompts in `5fe1882754a06d5a`). Either
OpenAI's best-effort cache is missing most of the time on this path, or something
in mu's codex request is not byte-stable across calls. **Lead, not conclusion** —
`ContextAssembly.prefix_hash` exists for exactly this diagnosis but only 20
sessions in the archive carry it.

## Running

```sh
./run cache_sim.py <root-or-session.jsonl>... [--only-compacted] [--validate] [--json]
./run cache_sim.py ~/.local/share/mu/events --only-compacted --validate --top 15
./run cache_sim.py <root> --calls-out calls.jsonl        # one JSON row per model call
./run cache_sim.py <root> --k 2                         # add a custom K scenario
```

stdlib only (no duckdb/polars); ~1 s per 25k-line session. With no paths it
falls back to `engine.MU_EVENTS` from `config.toml`.

## Next

- Plug alternative compaction policies into the replay (the snapshot is the
  metric to compare against — the operator's original intent for logging it).
- Record eviction cause + margin in `CompactionDecision` so recall hits can be
  labelled (see the 2026-10-04 cc discussion); the `r=session` column then starts
  to move.
- Chase the codex ratio with `prefix_hash` diffs on a fresh session.
