#!/usr/bin/env python3
"""Behavior-signature sweep report — a DATED markdown file, not a dashboard panel.

The sweep's read surface, by explicit operator decision (2026-08-11): the dashboard
accumulated too many undated lists and silently-capped panels to be trusted, so the
sweep reports here instead. Every row carries a date; nothing is silently truncated
without saying so.

Two lanes:
  semantic — behavior-judge verdicts (judge_store), the v2 sweep classes first.
      Verdicts are per-session; their date is the session's LAST activity in the
      archive (per-turn attribution of judge evidence is a v2 of this report).
  lexical  — signatures.py per-turn detectors: repetition rate bucketed by the
      turn's OWN ET hour and context size, and cross-session day-closing template
      pairs. Turn-accurate timestamps throughout.

Run:  ./run behavior_report.py [out.md]     (default ~/mu-stats/behavior-signatures.md)
"""

import os
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime

import engine
import judge_store
import signatures
from scans import ET

SWEEP_CLASSES = ["performative_closing", "outcome_prediction", "rule_echo"]
OUT = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/mu-stats/behavior-signatures.md")
TOP_PAIRS = 12
TOP_SESSIONS = 10


def hour_band(h):
    for lo, hi in ((0, 8), (8, 12), (12, 17), (17, 20), (20, 24)):
        if lo <= h < hi:
            return f"{lo:02d}-{hi:02d}"
    return "??"


def ctx_band(tok):
    if not tok:
        return "n/a"
    if tok < 50_000:
        return "<50k"
    if tok < 150_000:
        return "50-150k"
    if tok < 300_000:
        return "150-300k"
    return ">=300k"


def _et_date(ms):
    return datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone(ET).strftime("%Y-%m-%d")


def semantic_section(last_ms):
    """Per-class verdict counts + the dated list of firing sessions."""
    lines = ["## Semantic lane — behavior-judge verdicts", ""]
    verdicts = judge_store.read_verdicts(only_occurred=False)
    if not verdicts:
        return lines + ["_No verdicts in the store yet (judge cron has not run here)._", ""]
    by_class = defaultdict(lambda: [0, 0])  # class -> [judged, occurred]
    firing = defaultdict(list)
    for v in verdicts:
        c = by_class[v["behavior"]]
        c[0] += 1
        if v.get("occurred"):
            c[1] += 1
            firing[v["behavior"]].append(v)
    order = SWEEP_CLASSES + sorted(k for k in by_class if k not in SWEEP_CLASSES)
    lines += ["| class | judged | occurred | rate |", "|---|---|---|---|"]
    for cls in order:
        if cls not in by_class:
            lines.append(f"| {cls} | 0 | 0 | — _(backfill pending)_ |")
            continue
        judged, occ = by_class[cls]
        lines.append(f"| {cls} | {judged} | {occ} | {100 * occ / judged:.1f}% |")
    lines.append("")
    for cls in SWEEP_CLASSES:
        rows = firing.get(cls, [])
        if not rows:
            continue
        lines.append(f"### {cls} — firing sessions")
        for v in sorted(rows, key=lambda v: -(last_ms.get(v["session_ref"]) or 0)):
            when = last_ms.get(v["session_ref"])
            date = _et_date(when) if when else "date unknown"
            sev = v.get("severity") or "?"
            summary = (v.get("summary") or "").strip().replace("\n", " ")[:180]
            lines.append(f"- **{date}** `{v['session_ref']}` ({sev}) — {summary}")
        lines.append("")
    return lines


def lexical_section(turns_by_ref):
    lines = ["## Lexical lane — per-turn shingle detectors", ""]
    rec = signatures.scan_repetition(turns_by_ref)
    if not rec:
        return lines + ["_No assistant turns found._", ""]
    span = f"{min(r['et_date'] for r in rec)} .. {max(r['et_date'] for r in rec)}"
    lines.append(
        f"{len(rec)} substantial assistant turns across {len(turns_by_ref)} sessions ({span})."
    )
    lines.append("")

    def table(keyfn, title):
        # Fleet-split: the after-hours question is about the cc fleet's serving path;
        # mu's local-model loop repetition is an order of magnitude higher and would
        # drown it in a merged table.
        agg = defaultdict(lambda: [0, 0])
        for r in rec:
            a = agg[(r["ref"].split(":", 1)[0], keyfn(r))]
            a[0] += 1
            a[1] += r["repeat"]
        out = [
            f"### Repetition rate by {title}",
            "",
            "| fleet | band | turns | repeats | rate |",
            "|---|---|---|---|---|",
        ]
        for fleet, k in sorted(agg):
            n, rep = agg[(fleet, k)]
            out.append(f"| {fleet} | {k} | {n} | {rep} | {100 * rep / n:.2f}% |")
        out.append("")
        return out

    lines += table(lambda r: hour_band(r["et_hour"]), "ET hour of the turn")
    lines += table(lambda r: ctx_band(r["ctx"]), "context size of the turn")

    counts: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    last_date: dict[str, str] = {}
    for r in rec:
        c = counts[r["ref"]]
        c[0] += 1
        c[1] += int(r["repeat"])
        if r["et_date"] > last_date.get(r["ref"], ""):
            last_date[r["ref"]] = r["et_date"]
    top = [
        (ref, v)
        for ref, v in sorted(counts.items(), key=lambda kv: -kv[1][1])[:TOP_SESSIONS]
        if v[1]
    ]
    if top:
        lines += [f"### Most-repetitive sessions (top {len(top)})", ""]
        for ref, (n, rep) in top:
            lines.append(
                f"- **{last_date.get(ref, '?')}** `{ref}` — {rep}/{n} turns repeated "
                f"({100 * rep / n:.1f}%)"
            )
        lines.append("")

    closings = signatures.day_closings(turns_by_ref)
    pairs = signatures.closing_pairs(closings)
    # Cluster the pair graph (union-find on connected components): one recurring
    # template produces O(n^2) pairs and would fill any top-N pair list with the
    # same boilerplate — a cluster prints once, with its size and date span.
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    key = {}
    for i, c in enumerate(closings):
        key[(c[0], c[1])] = i
    for p in pairs:
        a, b = key[(p["a_ref"], p["a_date"])], key[(p["b_ref"], p["b_date"])]
        parent[find(a)] = find(b)
    clusters = defaultdict(list)
    for p in pairs:
        clusters[find(key[(p["a_ref"], p["a_date"])])].append(p)
    ranked = []
    for members in clusters.values():
        refs = {p["a_ref"] for p in members} | {p["b_ref"] for p in members}
        dates = sorted({p["a_date"] for p in members} | {p["b_date"] for p in members})
        ranked.append((len(refs), dates, max(p["sim"] for p in members), members[0]))
    ranked.sort(key=lambda c: -c[0])
    lines += [
        f"### Cross-session closing templates (Jaccard >= {signatures.PAIR_THRESHOLD}, clustered)",
        "",
        f"{len(closings)} day-closings; {len(pairs)} similar pairs collapsing to "
        f"{len(ranked)} clusters; top {min(TOP_PAIRS, len(ranked))} by session count. "
        "These are TEMPLATE reuse unless proven otherwise (handoff blocks, error "
        "boilerplate); stylistic same-voice similarity has no lexical overlap and "
        "lives in the judge's performative_closing class.",
        "",
    ]
    for n_refs, dates, max_sim, sample in ranked[:TOP_PAIRS]:
        lines.append(f"- {n_refs} sessions, **{dates[0]} .. {dates[-1]}**, max sim {max_sim:.2f}")
        lines.append(f"    - {sample['a_preview'][:110]}")
    lines.append("")
    return lines


def main():
    con = engine.connect()
    turns_by_ref = signatures.assistant_turns(con)
    last_ms = {ref: max(t[0] for t in turns) for ref, turns in turns_by_ref.items() if turns}
    now = datetime.now(tz=ET).strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        "# Behavior-signature sweep",
        "",
        f"_Generated {now}. Semantic verdicts accrue via the daily judge cron "
        "(new sessions + a capped backfill of history); lexical detectors rescan the "
        "full archive each run._",
        "",
    ]
    lines += semantic_section(last_ms)
    lines += lexical_section(turns_by_ref)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, OUT)
    print(f"wrote {OUT} ({len(lines)} lines) at {time.strftime('%H:%M:%S')}")


if __name__ == "__main__":
    main()
