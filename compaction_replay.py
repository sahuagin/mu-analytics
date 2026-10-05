#!/usr/bin/env python3
"""Compare compaction policies by replaying the same session traces under each.

The event log is the fixed input; the policy is the variable. For every policy
the replay produces the post-compaction rope at each compaction point, the cache
prefill under the cache_sim scenarios, and the recall-miss proxy (tool calls the
agent re-issued whose result the policy had evicted). The logged policy is the
control arm, and its numbers equal what mu actually did.

Columns:
  compactions        compaction points replayed (same for every policy)
  tokens_after/logged  mean post-compaction rope size under the policy vs mu's own
                     logged tokens_after (the snapshot yardstick)
  jaccard            mean overlap of the policy's CUMULATIVE drop set with the logged
                     cumulative drop set (1.0 = identical; the port should be ~1)
  prefill flat/seg   prompt tokens re-prefilled across ALL calls under a prefix cache
                     / a segment cache (seg k=6 r=prev)
  dup_after_drop     tool calls identical to an earlier one whose result was NOT in
                     the rope at the time (the recompute the policy caused or, for
                     logged, the one the operator paid for)
  dup_while_live     identical re-issue while the result WAS in context (baseline
                     agent redundancy; a policy cannot change it)

Run:  ./run compaction_replay.py <root-or-session.jsonl>... [--policies logged,span-family-drop,lexical]
      ./run compaction_replay.py ~/.local/share/mu/events --only-compacted
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

import cache_sim as cs
import compaction_policies as cp

DEFAULT_POLICIES = ("logged", "span-family-drop", "lexical")


def run(paths: list[str], policies: tuple[str, ...], min_calls: int = 2) -> dict:
    out: dict[str, dict] = {}
    for name in policies:
        agg = cs.Agg()
        tot = cs.SessionStats()
        sessions = 0
        per_session: dict[str, dict] = {}
        for path in paths:
            policy = None if name == "logged" else cp.make(name)
            rows, stats = cs.simulate_session_full(
                path, cs.DEFAULT_SCENARIOS, policy=policy, calibration="none"
            )
            if len(rows) < min_calls:
                continue
            sessions += 1
            for r in rows:
                agg.add(r)
            for f in (
                "tool_calls",
                "dup_after_drop",
                "dup_while_live",
                "compactions",
                "tokens_after_total",
                "tokens_after_logged",
                "decision_jaccard_sum",
                "recon_mismatches",
            ):
                setattr(tot, f, getattr(tot, f) + getattr(stats, f))
            for tool, n in stats.dup_after_drop_by_tool.items():
                tot.dup_after_drop_by_tool[tool] = tot.dup_after_drop_by_tool.get(tool, 0) + n
            for tool, n in stats.dup_while_live_by_tool.items():
                tot.dup_while_live_by_tool[tool] = tot.dup_while_live_by_tool.get(tool, 0) + n
            per_session[rows[0].session] = {
                "compactions": stats.compactions,
                "dup_after_drop": stats.dup_after_drop,
                "dup_while_live": stats.dup_while_live,
                "prefill_flat": round(sum(r.prefill["flat"] for r in rows)),
            }
        d = agg.to_dict()
        n_c = max(tot.compactions, 1)
        d.update(
            {
                "sessions": sessions,
                "compactions": tot.compactions,
                "tokens_after_mean": round(tot.tokens_after_total / n_c),
                "tokens_after_logged_mean": round(tot.tokens_after_logged / n_c),
                "decision_jaccard_mean": (
                    round(tot.decision_jaccard_sum / n_c, 3) if name != "logged" else 1.0
                ),
                "tool_calls": tot.tool_calls,
                "dup_after_drop": tot.dup_after_drop,
                "dup_after_drop_by_tool": dict(
                    sorted(tot.dup_after_drop_by_tool.items(), key=lambda kv: -kv[1])
                ),
                "dup_while_live": tot.dup_while_live,
                "dup_while_live_by_tool": dict(
                    sorted(tot.dup_while_live_by_tool.items(), key=lambda kv: -kv[1])[:6]
                ),
                "per_session": per_session,
            }
        )
        out[name] = d
    return out


def render(res: dict) -> str:
    seg = "seg k=6 r=prev"
    hdr = (
        f"{'policy':<18}{'compactions':>12}{'tokens_after':>13}{'logged':>9}{'jaccard':>9}"
        f"{'prefill flat':>14}{'prefill seg':>13}{'dup_after_drop':>16}{'dup_live':>10}"
    )
    lines = [hdr]
    for name, d in res.items():
        lines.append(
            f"{name:<18}{d['compactions']:>12}{d['tokens_after_mean']:>13,}"
            f"{d['tokens_after_logged_mean']:>9,}{d['decision_jaccard_mean']:>9}"
            f"{d['prefill']['flat']:>14,}{d['prefill'].get(seg, 0):>13,}"
            f"{d['dup_after_drop']:>16}{d['dup_while_live']:>10}"
        )
    lines.append("")
    lines.append("dup_after_drop by tool (top 6):")
    for name, d in res.items():
        top = list(d.get("dup_after_drop_by_tool", {}).items())[:6]
        lines.append(f"  {name:<16} " + "  ".join(f"{t}={n}" for t, n in top))
    first = next(iter(res.values()), None)
    if first and first.get("dup_while_live_by_tool"):
        top = list(first["dup_while_live_by_tool"].items())[:4]
        lines.append(
            "dup_while_live by tool (same for every policy; polling tools dominate): "
            + "  ".join(f"{t}={n}" for t, n in top)
        )
    base = res.get("logged")
    if base:
        lines.append("")
        lines.append("vs logged:")
        for name, d in res.items():
            if name == "logged":
                continue
            dflat = d["prefill"]["flat"] - base["prefill"]["flat"]
            ddup = d["dup_after_drop"] - base["dup_after_drop"]
            dtok = d["tokens_after_mean"] - base["tokens_after_mean"]
            lines.append(
                f"  {name:<16} prefill_flat {dflat:+,}   dup_after_drop {ddup:+}   "
                f"tokens_after_mean {dtok:+,}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--policies", default=",".join(DEFAULT_POLICIES))
    ap.add_argument("--only-compacted", action="store_true")
    ap.add_argument("--min-calls", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--per-session", action="store_true", help="also list per-session dup counts")
    args = ap.parse_args(argv)

    paths = cs.discover(args.paths)
    if args.only_compacted:
        paths = [p for p in paths if cs.has_compaction(p)]
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        print("compaction_replay: no sessions matched", file=sys.stderr)
        return 1
    policies = tuple(x.strip() for x in args.policies.split(",") if x.strip())
    res = run(paths, policies, args.min_calls)
    if args.json:
        json.dump(res, sys.stdout, indent=1)
        print()
        return 0
    print(f"sessions={next(iter(res.values()))['sessions']} policies={','.join(policies)}")
    print(render(res))
    if args.per_session:
        print("\nper-session dup_after_drop (logged / others):")
        names = list(res)
        table: dict[str, list] = defaultdict(list)
        for name in names:
            for sid, d in res[name]["per_session"].items():
                table[sid].append(d["dup_after_drop"])
        for sid, vals in sorted(table.items(), key=lambda kv: -max(kv[1])):
            print(
                f"  {sid:<40} " + "  ".join(f"{n}={v}" for n, v in zip(names, vals, strict=False))
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
