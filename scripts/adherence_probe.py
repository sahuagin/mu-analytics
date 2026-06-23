#!/usr/bin/env python3
"""Directive-adherence / context-rot probe (standalone, raw-jsonl).

A throwaway-grade prototype that reads session logs DIRECTLY (no DuckDB `ev`
view, no .172) so it runs over the local subset for fast iteration. The real
run graduates these signals into features.py (over the unified `ev` view) and
executes on .172 over ~/ai-sessions. See docs/directive-adherence.md.

What it measures per session (both fleets):
  - context-size trajectory: initial (≈ system context), max, growth
  - compaction spots via the ROBUST signal (a sharp drop in per-turn context),
    not the sparse isCompactSummary marker
Fleets:
  - cc: usage.{input,cache_read,cache_creation}_tokens per assistant turn
        (split bench vs real by path, since the local corpus is bench-heavy)
  - mu: context_assembly.token_count_estimate per model call
"""
import glob
import json
import os
import statistics as st


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(p * len(xs))))] if xs else 0


def cc_trajectory(f):
    ctxs = []
    for line in open(f, errors="ignore"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        u = (msg.get("usage") if isinstance(msg, dict) else None) or o.get("usage")
        if isinstance(u, dict) and u.get("input_tokens") is not None:
            ctx = (
                u.get("input_tokens", 0)
                + u.get("cache_read_input_tokens", 0)
                + u.get("cache_creation_input_tokens", 0)
            )
            if ctx > 0:
                ctxs.append(ctx)
    return ctxs


def mu_trajectory(f):
    ctxs = []
    for line in open(f, errors="ignore"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        p = o.get("payload") if isinstance(o.get("payload"), dict) else None
        if p and p.get("kind") == "context_assembly":
            t = p.get("token_count_estimate")
            if isinstance(t, (int, float)) and t > 0:
                ctxs.append(int(t))
    return ctxs


def compactions(ctxs, drop_frac=0.30, drop_abs=20000):
    """A compaction = a turn whose context drops sharply from the prior turn."""
    return sum(
        1 for a, b in zip(ctxs, ctxs[1:]) if a > 0 and (a - b) >= max(drop_abs, drop_frac * a)
    )


def summarize(name, files, traj):
    rows = []
    for f in files:
        ctxs = traj(f)
        if len(ctxs) >= 2:
            rows.append((ctxs[0], max(ctxs), compactions(ctxs), len(ctxs)))
    if not rows:
        print(f"\n[{name}] no sessions (>=2 turns)")
        return
    firsts = [r[0] for r in rows]
    maxes = [r[1] for r in rows]
    comps = [r[2] for r in rows]
    print(f"\n[{name}] sessions(>=2 turns): {len(rows)}")
    print(f"  initial ctx : p10={q(firsts,.1):>7,}  med={int(st.median(firsts)):>7,}  p90={q(firsts,.9):>9,}")
    print(f"  max ctx     : med={int(st.median(maxes)):>7,}  p90={q(maxes,.9):>9,}  max={max(maxes):>10,}")
    print(f"  growth x    : med={st.median([m/f for f,m,_,_ in rows]):.2f}")
    nc = sum(1 for c in comps if c > 0)
    print(f"  compaction  : {nc} sessions ({100*nc//len(rows)}%), {sum(comps)} total spots")


def main():
    # Benchmarks excluded (path contains 'bench') — a flat control population, not
    # real work. The cc-bench floor (~19k initial) is recorded in the research log;
    # pass INCLUDE_BENCH=1 to resurface it for a one-off.
    cc = [f for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
          if os.environ.get("INCLUDE_BENCH") or "bench" not in f]
    mu = glob.glob(os.path.expanduser("~/.local/share/mu/events/*/session-*.jsonl"))
    summarize("cc-real", cc, cc_trajectory)
    summarize("mu", mu, mu_trajectory)


if __name__ == "__main__":
    main()
