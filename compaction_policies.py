#!/usr/bin/env python3
"""Counterfactual compaction policies for the cache_sim replay.

`cache_sim.simulate_session(..., policy=...)` calls `policy.compact(state, payload)`
at every `compaction_assembly` event instead of applying the logged decisions.
A policy returns decisions in the SAME JSON shape mu logs
(`{"action": "dropped", "span_id": ..., "reason": ...}`), so the replay, the
cache models and the metrics are identical for logged and counterfactual runs.
The logged `context_assembly` snapshot is the yardstick (its `tokens_after`,
`message_count`), which is why mu logs it.

Policies:
  logged            replay what mu actually did (the default; the control arm).
  span-family-drop  python port of mu-core's SpanFamilyDropPolicy (heuristic.rs):
                    tier 2 oldest tool clusters (+ their assistant), tier 3 old
                    assistant turns (+ trailing cluster), keep the 2 most recent
                    assistants, then call_id pair reconciliation. Tiers 1/4
                    (file loads, skill activations) live in the standing prefix and
                    never reach the message area here. Validates the hook: its
                    drop set should ~match the logged decisions.
  lexical           algorithmic relevance: score each evictable exchange unit by
                    IDF-weighted term overlap with the last few user messages and
                    evict the LOWEST-scoring units first until under target. Users
                    are never evicted; the 2 most recent assistants are kept. The
                    first non-model, non-positional relevance scorer — a strawman
                    to prove the comparison harness, not a recommendation.

Target: mu's loop passes `target_tokens = compaction_threshold / 2`
(agent/loop_/mod.rs). The logged `compaction_assembly.compaction_threshold` gives
it back; pre-mu-a79g events (threshold 0) fall back to the logged `tokens_after`.
Sizes: chars/4 of each span's flattened text, which is mu's own renderer estimate
(fit against the logged token_breakdown), so the port stops where mu stopped.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Protocol

from cache_sim import Msg, ReplayState

KEEP_RECENT_ASSISTANT = 2  # heuristic.rs KEEP_RECENT_ASSISTANT


class CompactionPolicy(Protocol):
    name: str

    def compact(self, st: ReplayState, p: dict) -> list[dict]: ...


def target_tokens(p: dict) -> float:
    thr = float(p.get("compaction_threshold") or 0)
    if thr > 0:
        return thr / 2.0
    return float(p.get("tokens_after") or 0)


def calibrated_sizes(st: ReplayState, live: list[Msg], p: dict) -> list[float]:
    """chars/4 of each span's flattened text — mu's renderer ruler (see
    cache_sim._assistant_text). One ruler for the policy's stop condition and for
    the replay's rope sizes, so a policy stops where mu would have stopped."""
    return [m.chars / 4.0 for m in live]


def _dropped(span_id: str, reason: str) -> dict:
    return {"action": "dropped", "span_id": span_id, "reason": reason}


# ── logged (control) ────────────────────────────────────────────────────────


class LoggedPolicy:
    name = "logged"

    def compact(self, st: ReplayState, p: dict) -> list[dict]:
        return list(p.get("decisions") or [])


# ── span-family-drop port ───────────────────────────────────────────────────


def _tool_clusters(live: list[Msg]) -> list[list[int]]:
    clusters: list[list[int]] = []
    i = 0
    while i < len(live):
        if live[i].role == "tool_result":
            c = []
            while i < len(live) and live[i].role == "tool_result":
                c.append(i)
                i += 1
            clusters.append(c)
        else:
            i += 1
    return clusters


@dataclass
class _Drops:
    live: list[Msg]
    sizes: list[float]
    target: float
    tokens_after: float
    dropped: list[bool]
    decisions: dict[int, dict]  # idx -> decision (dict keeps undrop simple)

    def under(self) -> bool:
        return self.tokens_after <= self.target

    def drop(self, i: int, reason: str) -> None:
        if self.dropped[i]:
            return
        self.dropped[i] = True
        self.tokens_after -= self.sizes[i]
        self.decisions[i] = _dropped(self.live[i].span_id, reason)

    def undrop(self, i: int) -> None:
        if not self.dropped[i]:
            return
        self.dropped[i] = False
        self.tokens_after += self.sizes[i]
        self.decisions.pop(i, None)


def _reconcile_tool_pairs(d: _Drops, preserved: set[int]) -> None:
    results_by_call: dict[str, list[int]] = defaultdict(list)
    for i, m in enumerate(d.live):
        if m.role == "tool_result" and m.call_id:
            results_by_call[m.call_id].append(i)
    if not results_by_call:
        return
    for i, m in enumerate(d.live):
        if m.role != "assistant" or not m.tool_calls:
            continue
        members = [i]
        for cid, _name, _args in m.tool_calls:
            members.extend(results_by_call.get(cid, []))
        if not any(d.dropped[j] for j in members):
            continue
        if i not in preserved:
            for j in members:
                d.drop(j, "tool-pair reconciliation (call_id): closed orphaned exchange unit")
        else:
            for j in members:
                d.undrop(j)


class SpanFamilyDropPort:
    name = "span-family-drop"

    def compact(self, st: ReplayState, p: dict) -> list[dict]:
        live = st.live()
        if not live:
            return []
        sizes = calibrated_sizes(st, live, p)
        target = target_tokens(p)
        # mu compares the WHOLE rope to target_tokens; the standing prefix (system,
        # project files, memory, tool schemas) counts even though it is never evicted.
        d = _Drops(live, sizes, target, sum(sizes) + st.prefix_tokens, [False] * len(live), {})
        if d.under():
            return []
        assistants = [i for i, m in enumerate(live) if m.role == "assistant"]
        preserved = set(assistants[-KEEP_RECENT_ASSISTANT:])

        # Tier 2: oldest tool clusters first, with the assistant that issued them.
        for cluster in _tool_clusters(live):
            if d.under():
                break
            first = cluster[0]
            if first > 0 and live[first - 1].role == "assistant":
                d.drop(first - 1, "assistant with orphaned tool_use (evicted with tool cluster)")
            for j in cluster:
                d.drop(j, "old tool call/result cluster")

        # Tier 3: old assistant turns (+ trailing cluster), oldest first.
        n = len(live)
        for i in range(n):
            if d.under():
                break
            if live[i].role == "assistant" and i not in preserved and not d.dropped[i]:
                d.drop(i, "old assistant turn")
                j = i + 1
                while j < n and live[j].role == "tool_result":
                    d.drop(j, "tool cluster orphaned by assistant drop")
                    j += 1

        _reconcile_tool_pairs(d, preserved)
        return [d.decisions[i] for i in sorted(d.decisions)]


# ── lexical relevance ───────────────────────────────────────────────────────

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]{2,}")
_STOP = frozenset(
    "the and for with that this from have will your you are not but can what when "
    "where which there their they them then than into out over under about also just "
    "like been being were was has had does did doing done should would could may might "
    "shall must need want let use used using make made get got set put run ran see saw "
    "true false null none some any all each every both either neither".split()
)


def terms(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(text) if t.lower() not in _STOP}


def exchange_units(live: list[Msg]) -> list[list[int]]:
    """Units: an assistant with its tool results (by call_id, else adjacency);
    lone assistants; tool results with no producer. Users are excluded (never evicted)."""
    results_by_call: dict[str, list[int]] = defaultdict(list)
    for i, m in enumerate(live):
        if m.role == "tool_result" and m.call_id:
            results_by_call[m.call_id].append(i)
    claimed: set[int] = set()
    units: list[list[int]] = []
    n = len(live)
    for i, m in enumerate(live):
        if m.role != "assistant":
            continue
        members = [i]
        if m.tool_calls:
            for cid, _n, _a in m.tool_calls:
                members.extend(j for j in results_by_call.get(cid, []) if j not in claimed)
        else:
            j = i + 1
            while j < n and live[j].role == "tool_result" and j not in claimed:
                members.append(j)
                j += 1
        claimed.update(members)
        units.append(sorted(set(members)))
    for i, m in enumerate(live):
        if m.role == "tool_result" and i not in claimed:
            units.append([i])
            claimed.add(i)
    return units


class LexicalRelevancePolicy:
    name = "lexical"

    def __init__(self, recent_user_messages: int = 3):
        self.recent_user_messages = recent_user_messages

    def compact(self, st: ReplayState, p: dict) -> list[dict]:
        live = st.live()
        if not live:
            return []
        sizes = calibrated_sizes(st, live, p)
        target = target_tokens(p)
        # mu compares the WHOLE rope to target_tokens; the standing prefix (system,
        # project files, memory, tool schemas) counts even though it is never evicted.
        d = _Drops(live, sizes, target, sum(sizes) + st.prefix_tokens, [False] * len(live), {})
        if d.under():
            return []
        assistants = [i for i, m in enumerate(live) if m.role == "assistant"]
        preserved = set(assistants[-KEEP_RECENT_ASSISTANT:])

        users = [m for m in live if m.role == "user"]
        intent = terms(" ".join(m.content for m in users[-self.recent_user_messages :]))
        units = exchange_units(live)
        # IDF over units so ubiquitous terms (paths, tool names) carry little weight.
        df: dict[str, int] = defaultdict(int)
        unit_terms: list[set[str]] = []
        for u in units:
            ts = terms(" ".join(live[i].content for i in u))
            unit_terms.append(ts)
            for t in ts:
                df[t] += 1
        n_units = max(len(units), 1)

        def score(ts: set[str]) -> float:
            return sum(math.log(1 + n_units / df[t]) for t in ts & intent)

        scored = []
        for k, u in enumerate(units):
            if any(i in preserved for i in u):
                continue
            s = score(unit_terms[k])
            scored.append((s, u[0], k))  # lowest score first; ties oldest first
        scored.sort()
        for s, _first, k in scored:
            if d.under():
                break
            for i in units[k]:
                d.drop(i, f"low lexical relevance (score={s:.2f})")
        _reconcile_tool_pairs(d, preserved)
        return [d.decisions[i] for i in sorted(d.decisions)]


REGISTRY: dict[str, type] = {
    "logged": LoggedPolicy,
    "span-family-drop": SpanFamilyDropPort,
    "lexical": LexicalRelevancePolicy,
}


def make(name: str) -> CompactionPolicy:
    try:
        return REGISTRY[name]()
    except KeyError as e:
        raise SystemExit(f"unknown policy {name!r}; known: {', '.join(REGISTRY)}") from e
