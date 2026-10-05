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
in mu's codex request is not byte-stable across calls.

**Resolved 2026-10-04** (investigation over all 4,237 codex sessions, 22,894 calls;
bead `mu-codex-cache-time-line-jcnx5`). Both, in different proportions:

- 48.4% of non-first codex calls report `cache_read = 0`; misses are all-or-nothing,
  every `cache_read` is a multiple of 128, no TTL or size signature (fastest
  follow-ups miss most). Append-only tool rounds, which should be ~100% cached, miss
  49% of the time.
- The decisive split is how the system prompt reaches the wire. Sessions with no
  System span (`mu ask`, review seats — 61% of calls) send
  `build_effective_system_prompt()`'s *"Current time: HH:MM UTC. Session has been
  running for N minutes."* as the **entire `instructions` field at byte 0**, and it
  rotates every minute: a minute boundary between consecutive calls gives **93%
  zero-cache** (n=910) vs 61% without (n=10,462). Sessions whose system span
  overflows into `input[0]` (constant `DEFAULT_INSTRUCTIONS`) miss only 14%.
- The residual is server-side: with byte-identical prompts, misses are still
  15–60% and strongly model-dependent in the same month on the same code path
  (gpt-6-astra 92% zero, gpt-5.5 33%), plausibly worsened by mu never sending
  `prompt_cache_key`. `Response.prompt_cache_diagnostics` (OpenAI's own per-call
  miss reasons) is deserialized by `mu-openai` and read by nothing.
- `prefix_hash` is absent on every codex call because `OpenaiProvider` keeps the
  default `NoCacheStrategy`, so `prefix_forensics` returns `None` — which is why the
  archive could not diagnose this on its own.

Fix order filed on the bead: log the diagnostics; move the time line out of the
cacheable prefix (a volatile tail item, not byte 0); send `prompt_cache_key`; give
the codex provider a boundary-emitting cache strategy so `prefix_hash` lands in
`context_assembly`. Re-run `cache_sim.py --validate` on fresh codex sessions after
the fix lands; the class-A minute-crossing hit rate should rise from 2% to the
same-minute baseline, and the fleet ratio from 0.056 toward the 0.6–0.75 band.

## Counterfactual compaction policies (the replay as a policy harness)

`cache_sim.simulate_session(..., policy=...)` hands every `compaction_assembly`
event to a policy instead of applying the logged decisions. The policy returns
decisions in the same JSON shape mu logs, so replay, cache models and metrics are
shared; the logged policy is the control arm and the `context_assembly` snapshot
stays the yardstick. This is what the snapshot was logged for.

Policies (`compaction_policies.py`):

- **logged** — what mu did.
- **span-family-drop** — Python port of mu-core's `SpanFamilyDropPolicy`: tier 2
  oldest tool clusters with the assistant that issued them, tier 3 old assistant
  turns with their trailing cluster, two most recent assistants preserved, call_id
  pair reconciliation last. Tiers 1 and 4 never reach the message area.
- **lexical** — the first non-model, non-positional relevance scorer: IDF-weighted
  term overlap between each exchange unit and the last three user messages, lowest
  score evicted first, users never evicted. A strawman to prove the harness.

Target: mu passes `target_tokens = compaction_threshold / 2`
(`agent/loop_/mod.rs`); the logged `compaction_threshold` gives it back.

**One ruler.** Policies need per-span sizes to know when to stop, and the replay
needs them to size the resulting rope. Fitting chars/4 against the logged
`token_breakdown` on 3,218 pre-compaction calls (where the live set is known
exactly) gives per-kind factors of 0.995 / 1.000 / 0.999 (assistant / tool_result /
user) with MAD under 0.5%, once assistant content is flattened the way
`assembly.rs::flatten_assistant` does (text blocks + `[tool_call:name(args)]`,
thinking excluded). So mu's renderer estimate *is* chars/4 of the flattened text,
and the replay uses that single ruler for every policy (`calibration="none"`).
Before this fix the assistant measurement included thinking blocks and JSON
overhead (factor 0.31, wildly dispersed), which made the port under-evict by 2×.

**Agreement metric.** Per-compaction Jaccard of drop sets is the wrong yardstick
for a counterfactual: after the first disagreement the sets diverge by
construction (a span the policy kept gets dropped at a later compaction than mu
dropped it) even when the policies agree. The report uses the *cumulative* drop
sets, which converge when they agree.

**Recall-miss proxy.** `dup_after_drop` counts tool calls identical (name +
arguments) to an earlier call whose result was not in the rope at the time. Under
the logged policy that is a recompute the operator actually paid for. The
keep-everything control moves every one of the probe session's 19 duplicates from
after-drop to while-live, which validates the measurement. `dup_while_live` is the
same re-issue while the result was in context: the agent's baseline redundancy,
dominated by polling tools in autonomous sessions, and not something retention can
change. Both are reported by tool name because a repeated `read` of the same path
is a recompute and a repeated `mailbox` poll is not.

### Results — 63 compacted sessions, 1,008 compaction points, 2026-10-05

| policy | tokens_after (mean) | mu logged | cumulative Jaccard | prefill flat | prefill seg k=6 | dup_after_drop | dup by tool |
|---|---|---|---|---|---|---|---|
| logged | 90,599 | 92,376 | 1.0 | 85.4 M | 28.2 M | **76** | read 48, bash 10, mailbox 6 |
| span-family-drop (port) | 91,767 | 92,376 | **0.953** | 84.2 M | 28.2 M | 74 | read 48, bash 8, mailbox 6 |
| lexical | 91,853 | 92,376 | 0.883 | **77.3 M** | 29.9 M | **99** | read 50, **bash 31**, mailbox 6 |

`dup_while_live` is ~68k for every policy, 66,964 of it `bash` in one autonomous
session polling with identical arguments. It is the agent's redundancy, not
retention's, and it is why the by-tool split exists.

Reading it:

- **The hook is validated.** The port reproduces mu's cumulative drop set at 0.95
  and its post-compaction size within 0.7%; the residual is cluster-granularity
  ties at the stop point. A policy run through the replay is now comparable to
  what mu actually did on the same traces.
- **mu's real recall-miss rate is measurable.** Across 1,008 compactions the agent
  re-issued 76 tool calls whose results compaction had evicted, 48 of them `read`
  of the same path. That is the recompute cost of `span-family-drop` today, and it
  is the number an archive-and-recall controller has to beat.
- **Naive relevance eviction is worse than positional eviction on recall misses.**
  The lexical scorer ends at the same size, re-prefills 9% less under a prefix cache
  (it tends to evict large low-overlap units, so the first gap lands later) and
  fragments slightly more under a segment cache, but it causes 23 more recomputes,
  almost all `bash` results the agent came back for. Recency is a strong relevance
  prior; a scorer that ignores it loses to the heuristic. This is the kind of
  result the harness exists to produce before anything ships in mu-core.
- The scorer that would be worth testing next is recency-weighted relevance with
  the exchange-unit structure kept, scored against the pairwise labels the new
  `EvictionCause`/`over_target_before` fields (mu PR #718) will start accruing.

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
- After the codex fix lands, re-validate on fresh codex sessions (see above).
