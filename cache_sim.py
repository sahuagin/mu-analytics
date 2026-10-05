#!/usr/bin/env python3
"""Prefix-cache simulator over mu session event logs.

Replays each session's rope, call by call, from the event JSONL alone and asks: how
many prompt tokens would the serving side have to prefill under different cache
protocols? The rope is reconstructed, not read — the log never stores the full span
list — and the reconstruction is checked against the per-call `context_assembly`
snapshot (message_count, span_count) so a divergence is reported, never silent.

Why: compaction and skill activation edit the INTERIOR of the prompt. A prefix cache
(Anthropic cache_control, OpenAI automatic caching, vLLM/SGLang radix caching) can
only reuse the longest unchanged prefix, so every interior edit re-prefills the whole
tail. A segment-addressed cache (the harness names spans by stable id + content hash,
the server relocates surviving spans — cf. "Suffix Cache Reuse", arXiv 2609.37725)
can reuse spans that moved. This script puts a number on the gap for OUR workload,
from OUR traces, before anyone writes a line of server code.

Reconstruction (validated: 0 message_count mismatches on the probe session):
  - the agent loop's `messages` Vec is replayed from `continuation_seeded.messages`
    (a resumed head's inherited history) + `user_message` / `assistant_message_event`
    / `tool_result` events, in log order; span ids follow mu-core's assembly.rs
    (`msg-{idx}-user`, `msg-{idx}-assistant`, `msg-{idx}-tool-result:{call_id}`).
    Indices are positions in the uncompacted Vec and are stable across compaction.
  - `compaction_assembly.decisions` remove dropped / absorbed spans and add the
    summary span; `context_cleared` resets everything.
  - the non-message prefix (system prompt, project files, memory recall, tool
    schemas) is NOT itemized in the log beyond `first_span_ids` (5) + `tool_count`.
    It is modelled as two pseudo-spans: a static block and a tool-schema block, each
    hashed over what the log does expose, sized from `token_breakdown`. Coarse: a
    one-tool change recomputes the whole schema block under every model.
  - per-span token weights: chars/4, rescaled per kind per call so the kind totals
    match the logged `token_breakdown` (the renderer's own estimate). Summary spans
    take the logged `compaction_summary` total (their text is not in the log).

Cache models (per consecutive model call within a session):
  flat          longest common prefix by (span id, content hash); tail re-prefilled.
                Upper bound for every prefix-only cache.
  seg k=K r=R   flat + up to K relocated RUNS (contiguous surviving stretches, the
                paper's "chunks"; K=6 is its default) from the residency set R, where
                R = "prev" (only the previous call's spans are resident) or
                "session" (every span ever sent stays resident). K=inf is the ideal.
                Relocation is the approximate operation; it is counted separately.

Validation hooks: the assistant usage that follows each call carries the provider's
own input / cache_read figures. Where present, the report compares simulated flat
prefill against reported uncached input (`--validate`), honoring the session's
`usage_semantics.cache_read_in_input`.

Run:  ./run cache_sim.py <root-or-session.jsonl>... [--only-compacted] [--json]
      ./run cache_sim.py ~/.local/share/mu/events --only-compacted --validate
      ./run cache_sim.py <root> --calls-out calls.jsonl     # per-call rows
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field

MESSAGE_KINDS = ("user", "assistant", "tool_result")
SUMMARY_KIND = "compaction_summary"
# Event kinds the replay consumes; everything else (provider_status_update is ~85%
# of lines) is skipped before json.loads.
_NEEDED = (
    "user_message",
    "assistant_message_event",
    "tool_result",
    "continuation_seeded",
    "context_cleared",
    "compaction_assembly",
    "context_assembly",
    "session_created",
)
_NEEDLES = tuple(f'"kind":"{k}"' for k in _NEEDED) + tuple(f'"kind": "{k}"' for k in _NEEDED)

INF = float("inf")


# ── rope primitives ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Seg:
    """One span as the cache sees it: identity, content fingerprint, weight."""

    id: str
    hash: str
    kind: str
    tokens: float

    @property
    def key(self) -> tuple[str, str]:
        return (self.id, self.hash)


def _h(*parts: object) -> str:
    m = hashlib.blake2b(digest_size=8)
    for p in parts:
        m.update(json.dumps(p, sort_keys=True, default=str).encode())
        m.update(b"\x00")
    return m.hexdigest()


@dataclass
class Msg:
    idx: int
    role: str  # user | assistant | tool_result
    span_id: str
    chars: int
    hash: str


@dataclass
class Scenario:
    name: str
    k: float  # relocations per call; 0 = flat, inf = unbounded
    residency: str  # "prev" | "session"

    @property
    def is_flat(self) -> bool:
        return self.k == 0


DEFAULT_SCENARIOS = (
    Scenario("flat", 0, "prev"),
    Scenario("seg k=6 r=prev", 6, "prev"),
    Scenario("seg k=inf r=prev", INF, "prev"),
    Scenario("seg k=inf r=session", INF, "session"),
)


@dataclass
class CallRow:
    session: str
    call_id: int
    provider: str
    model: str
    spans: int
    tokens: float
    compaction: bool
    prefix_changed: bool
    first_call: bool
    prefill: dict[str, float]
    relocated: dict[str, float]
    reported_prompt: int | None = None
    reported_uncached: int | None = None
    recon_ok: bool = True


# ── cache models ────────────────────────────────────────────────────────────


def common_prefix_len(prev: list[Seg], cur: list[Seg]) -> int:
    n = 0
    for a, b in zip(prev, cur, strict=False):
        if a.key != b.key:
            break
        n += 1
    return n


def simulate_call(
    prev: list[Seg] | None,
    cur: list[Seg],
    resident: set[tuple[str, str]],
    sc: Scenario,
) -> tuple[float, float]:
    """Return (prefill_tokens, relocated_tokens) for one call under `sc`.

    `resident` is the set of (id, hash) keys the server still holds for this
    scenario's residency rule; the caller maintains it between calls.
    """
    total = sum(s.tokens for s in cur)
    if prev is None:
        return total, 0.0
    lcp = common_prefix_len(prev, cur)
    hit = sum(s.tokens for s in cur[:lcp])
    if sc.is_flat:
        return total - hit, 0.0
    tail = cur[lcp:]
    # Spans after the divergence that the server still holds can be relocated
    # (re-rotated into their new position). K bounds the number of RUNS moved,
    # not spans: a contiguous stretch of survivors that was also contiguous in the
    # previous prompt shifts by one offset and is one relocation (the paper's
    # "chunk"). A resident span with no previous-prompt neighbour is a run of one.
    prev_pos = {s.key: i for i, s in enumerate(prev)}
    runs: list[float] = []
    run_tokens = 0.0
    last_pos: int | None = None
    for s in tail:
        pos = prev_pos.get(s.key)
        if s.key not in resident:
            if run_tokens:
                runs.append(run_tokens)
            run_tokens, last_pos = 0.0, None
            continue
        contiguous = pos is not None and last_pos is not None and pos == last_pos + 1
        if run_tokens and not contiguous:
            runs.append(run_tokens)
            run_tokens = 0.0
        run_tokens += s.tokens
        last_pos = pos
    if run_tokens:
        runs.append(run_tokens)
    runs.sort(reverse=True)
    if sc.k != INF:
        runs = runs[: int(sc.k)]
    relocated = sum(runs)
    return total - hit - relocated, relocated


# ── replay ──────────────────────────────────────────────────────────────────


@dataclass
class _ReplayState:
    msgs: list[Msg] = field(default_factory=list)
    dropped: set[str] = field(default_factory=set)
    absorbed: set[str] = field(default_factory=set)
    summaries: list[str] = field(default_factory=list)
    pending_compaction: int | None = None
    pending_call: dict | None = None  # context_assembly awaiting its usage
    cache_read_in_input: bool = False
    provider: str = ""
    model: str = ""

    def reset(self) -> None:
        self.msgs.clear()
        self.dropped.clear()
        self.absorbed.clear()
        self.summaries.clear()

    def add(self, role: str, chars: int, h: str, call_id: str | None = None) -> None:
        n = len(self.msgs)
        sid = f"msg-{n}-tool-result:{call_id}" if role == "tool_result" else f"msg-{n}-{role}"
        self.msgs.append(Msg(n, role, sid, chars, h))

    def live(self) -> list[Msg]:
        return [
            m for m in self.msgs if m.span_id not in self.dropped and m.span_id not in self.absorbed
        ]


def _content_len(obj: object) -> int:
    if isinstance(obj, str):
        return len(obj)
    return len(json.dumps(obj, separators=(",", ":"), default=str))


def _seed_message(st: _ReplayState, m: dict) -> None:
    role = m.get("role")
    if role == "user":
        c = m.get("content", "")
        st.add("user", _content_len(c), _h("user", c))
    elif role == "assistant":
        c = m.get("content", [])
        st.add("assistant", _content_len(c), _h("assistant", c))
    elif role == "tool_result":
        c = m.get("content", "")
        st.add(
            "tool_result",
            _content_len(c),
            _h("tool", c, m.get("is_error", False)),
            m.get("call_id"),
        )


def build_rope(st: _ReplayState, p: dict) -> tuple[list[Seg], bool]:
    """Project replay state + one context_assembly payload into the ordered rope.

    Returns (segments, recon_ok). recon_ok is False when the replayed message
    count disagrees with the snapshot — the row is still produced but flagged.
    """
    bd: dict[str, float] = {k: float(v) for k, v in (p.get("token_breakdown") or {}).items()}
    live = st.live()

    # Per-kind calibration: chars/4 rescaled so kind totals match the renderer's.
    raw_by_kind: dict[str, float] = defaultdict(float)
    for m in live:
        raw_by_kind[m.role] += m.chars / 4.0
    scale = {}
    for kind in MESSAGE_KINDS:
        logged = bd.get(kind)
        raw = raw_by_kind.get(kind, 0.0)
        scale[kind] = (logged / raw) if (logged and raw > 0) else 1.0

    prefix_kinds = {k: v for k, v in bd.items() if k not in MESSAGE_KINDS and k != SUMMARY_KIND}
    tool_tokens = prefix_kinds.pop("tool_schema", 0.0)
    static_tokens = sum(prefix_kinds.values())
    first_ids = p.get("first_span_ids") or []
    static_ids = [s for s in first_ids if not s.startswith("tool-schema:")]
    tool_count = int(p.get("tool_count") or 0)

    segs: list[Seg] = [
        Seg("prefix:static", _h(static_ids, sorted(prefix_kinds)), "prefix", static_tokens),
        Seg("prefix:tool-schemas", _h(tool_count, round(tool_tokens)), "tool_schema", tool_tokens),
    ]
    # Summary spans sit at the position of the earliest absorbed span; the log
    # does not carry that position, so they are placed before the surviving
    # messages (where compaction puts them in practice: the absorbed material is
    # the OLD part of the conversation).
    n_sum = len(st.summaries)
    sum_tokens = bd.get(SUMMARY_KIND, 0.0) / n_sum if n_sum else 0.0
    for sid in st.summaries:
        segs.append(Seg(sid, _h("summary", sid), SUMMARY_KIND, sum_tokens))
    for m in live:
        segs.append(Seg(m.span_id, m.hash, m.role, m.chars / 4.0 * scale[m.role]))

    recon_ok = len(st.msgs) == int(p.get("message_count") or -1)
    return segs, recon_ok


def iter_events(path: str):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not any(n in line for n in _NEEDLES):
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def simulate_session(
    path: str,
    scenarios: tuple[Scenario, ...] = DEFAULT_SCENARIOS,
    session_label: str | None = None,
) -> list[CallRow]:
    label = session_label or _session_label(path)
    st = _ReplayState()
    rows: list[CallRow] = []
    prev: list[Seg] | None = None
    resident: dict[str, set[tuple[str, str]]] = {sc.name: set() for sc in scenarios}

    def flush_usage(usage: dict | None) -> None:
        if st.pending_call is None or not rows:
            return
        row = rows[-1]
        if usage and row.call_id == st.pending_call.get("model_call_id"):
            inp = int(usage.get("input_tokens") or 0)
            cr = usage.get("cache_read_input_tokens")
            # A provider that never reports cache reads (vllm143: 100% null) says
            # nothing about caching; counting its whole prompt as uncached would
            # make the validation ratio meaningless. Only rows WITH a figure count.
            if cr is not None:
                cr = int(cr)
                if st.cache_read_in_input:
                    row.reported_prompt = inp
                    row.reported_uncached = inp - cr
                else:
                    row.reported_prompt = inp + cr
                    row.reported_uncached = inp
        st.pending_call = None

    for e in iter_events(path):
        p = e.get("payload") or {}
        k = p.get("kind")
        if k == "session_created":
            st.provider = p.get("provider_kind", "") or ""
            st.model = p.get("model", "") or ""
            sem = p.get("usage_semantics") or {}
            st.cache_read_in_input = bool(sem.get("cache_read_in_input"))
        elif k == "user_message":
            c = p.get("content", "")
            st.add("user", _content_len(c), _h("user", c))
        elif k == "assistant_message_event":
            msg = p.get("message") or {}
            flush_usage(msg.get("usage"))
            c = msg.get("content", [])
            st.add("assistant", _content_len(c), _h("assistant", c))
        elif k == "tool_result":
            c = p.get("content", "")
            st.add(
                "tool_result",
                _content_len(c),
                _h("tool", c, p.get("is_error", False)),
                p.get("call_id"),
            )
        elif k == "continuation_seeded":
            for m in p.get("messages") or []:
                _seed_message(st, m)
        elif k == "context_cleared":
            st.reset()
            prev = None
            for s in resident.values():
                s.clear()
        elif k == "compaction_assembly":
            for d in p.get("decisions") or []:
                a = d.get("action")
                if a == "dropped":
                    st.dropped.add(d["span_id"])
                elif a == "summarized":
                    st.absorbed.update(d.get("absorbed_span_ids") or [])
                    st.summaries.append(d["summary_span_id"])
            st.pending_compaction = p.get("model_call_id")
        elif k == "context_assembly":
            flush_usage(None)
            cur, ok = build_rope(st, p)
            call_id = int(p.get("model_call_id") or 0)
            prefill: dict[str, float] = {}
            reloc: dict[str, float] = {}
            for sc in scenarios:
                pf, rl = simulate_call(prev, cur, resident[sc.name], sc)
                prefill[sc.name] = pf
                reloc[sc.name] = rl
                if sc.residency == "prev":
                    resident[sc.name] = {s.key for s in cur}
                else:
                    resident[sc.name].update(s.key for s in cur)
            prefix_changed = prev is not None and (prev[0].key, prev[1].key) != (
                cur[0].key,
                cur[1].key,
            )
            rows.append(
                CallRow(
                    session=label,
                    call_id=call_id,
                    provider=p.get("provider_kind") or st.provider,
                    model=p.get("model") or st.model,
                    spans=len(cur),
                    tokens=sum(s.tokens for s in cur),
                    compaction=st.pending_compaction == call_id,
                    prefix_changed=prefix_changed,
                    first_call=prev is None,
                    prefill=prefill,
                    relocated=reloc,
                    recon_ok=ok,
                )
            )
            st.pending_compaction = None
            st.pending_call = p
            prev = cur
    return rows


def _session_label(path: str) -> str:
    d, f = os.path.split(path)
    return f"{os.path.basename(d)}:{os.path.splitext(f)[0]}"


# ── discovery / aggregation ─────────────────────────────────────────────────


def discover(paths: list[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            out.extend(glob.glob(os.path.join(p, "**", "session-*.jsonl"), recursive=True))
        elif os.path.isfile(p):
            out.append(p)
    return sorted(set(out))


def has_compaction(path: str) -> bool:
    needle = '"compaction_assembly"'
    with open(path, encoding="utf-8", errors="replace") as fh:
        return any(needle in line for line in fh)


@dataclass
class Agg:
    calls: int = 0
    compaction_calls: int = 0
    prefix_changes: int = 0
    tokens: float = 0.0
    prefill: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    relocated: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    recon_bad: int = 0
    # validation (flat vs reported), only rows with reported usage
    val_n: int = 0
    val_sim_uncached: float = 0.0
    val_rep_uncached: float = 0.0
    val_rep_prompt: float = 0.0

    def add(self, r: CallRow) -> None:
        self.calls += 1
        self.compaction_calls += r.compaction
        self.prefix_changes += r.prefix_changed
        self.tokens += r.tokens
        self.recon_bad += not r.recon_ok
        for k, v in r.prefill.items():
            self.prefill[k] += v
        for k, v in r.relocated.items():
            self.relocated[k] += v
        if r.reported_uncached is not None and not r.first_call:
            self.val_n += 1
            self.val_sim_uncached += r.prefill.get("flat", 0.0)
            self.val_rep_uncached += r.reported_uncached
            self.val_rep_prompt += r.reported_prompt or 0

    def to_dict(self) -> dict:
        d: dict[str, object] = {
            "calls": self.calls,
            "compaction_calls": self.compaction_calls,
            "prefix_changes": self.prefix_changes,
            "tokens_sent": round(self.tokens),
            "recon_mismatches": self.recon_bad,
            "prefill": {k: round(v) for k, v in self.prefill.items()},
            "relocated": {k: round(v) for k, v in self.relocated.items()},
        }
        flat = self.prefill.get("flat", 0.0)
        d["savings_vs_flat"] = {
            k: (round(1 - v / flat, 4) if flat > 0 else None) for k, v in self.prefill.items()
        }
        if self.val_n:
            d["validation"] = {
                "calls": self.val_n,
                "sim_flat_uncached": round(self.val_sim_uncached),
                "reported_uncached": round(self.val_rep_uncached),
                "reported_prompt": round(self.val_rep_prompt),
                "sim_over_reported": (
                    round(self.val_sim_uncached / self.val_rep_uncached, 3)
                    if self.val_rep_uncached > 0
                    else None
                ),
            }
        return d


def aggregate(rows: list[CallRow]) -> dict:
    fleet = Agg()
    by_provider: dict[str, Agg] = defaultdict(Agg)
    by_session: dict[str, Agg] = defaultdict(Agg)
    compaction_only = Agg()
    steady = Agg()
    for r in rows:
        fleet.add(r)
        by_provider[r.provider].add(r)
        by_session[r.session].add(r)
        (compaction_only if r.compaction else steady).add(r)
    return {
        "fleet": fleet.to_dict(),
        "compaction_calls": compaction_only.to_dict(),
        "steady_calls": steady.to_dict(),
        "by_provider": {k: v.to_dict() for k, v in sorted(by_provider.items())},
        "by_session": {k: v.to_dict() for k, v in sorted(by_session.items())},
    }


# ── report ──────────────────────────────────────────────────────────────────


def _pct(x: float | None) -> str:
    return "   n/a" if x is None else f"{100 * x:6.1f}%"


def _fmt_block(title: str, d: dict, scenario_names: list[str]) -> list[str]:
    lines = [
        f"{title}: calls={d['calls']} compaction_calls={d['compaction_calls']} "
        f"prefix_changes={d['prefix_changes']} tokens_sent={d['tokens_sent']:,} "
        f"recon_mismatches={d['recon_mismatches']}"
    ]
    for name in scenario_names:
        pf = d["prefill"].get(name, 0)
        rl = d["relocated"].get(name, 0)
        sv = d["savings_vs_flat"].get(name)
        frac = (pf / d["tokens_sent"]) if d["tokens_sent"] else 0.0
        lines.append(
            f"  {name:<22} prefill={pf:>14,}  ({100 * frac:5.1f}% of sent)"
            f"  relocated={rl:>12,}  savings_vs_flat={_pct(sv)}"
        )
    v = d.get("validation")
    if v:
        lines.append(
            f"  validation (flat vs provider-reported, {v['calls']} calls): "
            f"sim_uncached={v['sim_flat_uncached']:,} reported_uncached={v['reported_uncached']:,} "
            f"ratio={v['sim_over_reported']}"
        )
    return lines


def render(agg: dict, scenarios: tuple[Scenario, ...], top_sessions: int = 10) -> str:
    names = [s.name for s in scenarios]
    out: list[str] = []
    out += _fmt_block("FLEET", agg["fleet"], names)
    out += _fmt_block("COMPACTION CALLS (the interior-edit case)", agg["compaction_calls"], names)
    out += _fmt_block("STEADY CALLS (append-only turns)", agg["steady_calls"], names)
    out.append("BY PROVIDER")
    for prov, d in agg["by_provider"].items():
        out += ["  " + ln for ln in _fmt_block(prov, d, names)]
    sess = sorted(
        agg["by_session"].items(),
        key=lambda kv: -(kv[1]["prefill"].get("flat", 0) - kv[1]["prefill"].get(names[-1], 0)),
    )[:top_sessions]
    out.append(f"TOP SESSIONS by tokens saved under '{names[-1]}' vs flat")
    for sid, d in sess:
        saved = d["prefill"].get("flat", 0) - d["prefill"].get(names[-1], 0)
        out.append(
            f"  {sid:<40} calls={d['calls']:>5} compactions={d['compaction_calls']:>3} "
            f"flat={d['prefill'].get('flat', 0):>12,} saved={saved:>12,} "
            f"({_pct(d['savings_vs_flat'].get(names[-1]))})"
        )
    return "\n".join(out)


# ── cli ─────────────────────────────────────────────────────────────────────


def _default_paths() -> list[str]:
    try:
        import engine  # repo config-driven glob (mu_events_root)

        return [engine.MU_EVENTS]
    except Exception:
        return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("paths", nargs="*", help="session-*.jsonl files or roots to search (recursive)")
    ap.add_argument(
        "--only-compacted", action="store_true", help="skip sessions with no compaction_assembly"
    )
    ap.add_argument("--min-calls", type=int, default=2, help="skip sessions with fewer model calls")
    ap.add_argument("--limit", type=int, default=0, help="stop after N sessions (0 = all)")
    ap.add_argument("--k", type=float, default=None, help="add a custom 'seg k=K r=prev' scenario")
    ap.add_argument("--json", action="store_true", help="emit the aggregate as JSON")
    ap.add_argument("--calls-out", help="write one JSON row per model call to this path")
    ap.add_argument("--top", type=int, default=10, help="sessions to list in the text report")
    ap.add_argument(
        "--validate",
        action="store_true",
        help="also print per-provider flat-vs-reported ratio detail",
    )
    args = ap.parse_args(argv)

    paths = discover(args.paths or _default_paths())
    if not paths:
        print("cache_sim: no session-*.jsonl found; pass a root or file", file=sys.stderr)
        return 2
    scenarios = DEFAULT_SCENARIOS
    if args.k is not None:
        scenarios = scenarios + (Scenario(f"seg k={int(args.k)} r=prev", args.k, "prev"),)

    rows: list[CallRow] = []
    n_sessions = 0
    calls_fh = open(args.calls_out, "w") if args.calls_out else None
    try:
        for path in paths:
            if args.only_compacted and not has_compaction(path):
                continue
            srows = simulate_session(path, scenarios)
            if len(srows) < args.min_calls:
                continue
            n_sessions += 1
            rows.extend(srows)
            if calls_fh:
                for r in srows:
                    calls_fh.write(json.dumps(r.__dict__, default=float) + "\n")
            if args.limit and n_sessions >= args.limit:
                break
    finally:
        if calls_fh:
            calls_fh.close()

    if not rows:
        print("cache_sim: no sessions matched", file=sys.stderr)
        return 1
    agg = aggregate(rows)
    agg["sessions"] = n_sessions
    if args.json:
        json.dump(agg, sys.stdout, indent=1)
        print()
    else:
        print(f"sessions={n_sessions} files_scanned={len(paths)}")
        print(render(agg, scenarios, args.top))
        if args.validate:
            print(
                "VALIDATION DETAIL (sim flat uncached / provider-reported uncached; ~1.0 = prefix-cache model matches provider)"
            )
            for prov, d in agg["by_provider"].items():
                v = d.get("validation")
                if v:
                    print(f"  {prov:<14} calls={v['calls']:>6} ratio={v['sim_over_reported']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
