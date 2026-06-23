#!/usr/bin/env python3
"""Violation/context x outcome (operator frustration) — EXPOSURE-NORMALIZED.

Round-5 finding: frustration PRESENCE (>=1 marker) is an exposure artifact — it
rises 26%->100% with session size because a long session has many operator
messages and a flat ~0.7% per-message marker rate makes >=1 marker near-certain.
So presence-lift is confounded by length and not causal.

This version uses the RATE = frustration markers / operator messages, which
controls for exposure, and stratifies by session size so a real context-rot signal
can be separated from "long sessions are just harder." Still operator-language only
(the "expensive grep" outcome); the proper estimate is degradation.py's regression
on the analytics host corpus. Markers verbatim from mu-analytics scans.py.
"""

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adherence_probe as P  # noqa: E402
import violations as V  # noqa: E402

NEG = re.compile(
    "|".join(
        [
            r"relitigat",
            r"whose money",
            r"why are (we|you)",
            r"that'?s not what",
            r"\bno\.\s*$",
            r"\bstop\b",
            r"\bwtf\b",
            r"wrong again",
            r"\bi said\b",
            r"you keep",
            r"hallucinat",
            r"not fun",
            r"\bbroken\b",
            r"don'?t do",
            r"\bagain\?",
            r"i didn'?t ask",
            r"burn(ing)? (money|tokens)",
            r"please don'?t",
            r"do not commit",
            r"stop reinforcing",
            r"don'?t feel like going (through|over)",
            r"we'?ve already done (it|this)",
            r"look in your memory",
            r"AI.?spla[in]",
            r"why restate",
            r"\bto be superior\b",
            r"i (already|just) (verified|gave|told|ran|showed)",
            r"run it all again",
            r"making more work for me",
        ]
    ),
    re.I,
)


def cc_user(f):
    """(concatenated operator-typed text, n_operator_messages). tool_result excluded."""
    parts, n = [], 0
    for line in open(f, errors="ignore"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "user":
            continue
        c = (o.get("message") or {}).get("content")
        if isinstance(c, str) and c.strip():
            parts.append(c)
            n += 1
        elif isinstance(c, list):
            txt = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
            if any(t.strip() for t in txt):
                parts.append("\n".join(txt))
                n += 1
    return "\n".join(parts), n


def rate(rows):
    msgs = sum(r["msgs"] for r in rows)
    return (sum(r["neg"] for r in rows) / msgs) if msgs else 0.0


def main():
    cc = [
        f for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")) if "bench" not in f
    ]
    rows = []
    for f in cc:
        t = V.cc_tools(f)
        if not t:
            continue
        text, nmsgs = cc_user(f)
        if nmsgs == 0:
            continue
        ctx = P.cc_trajectory(f)
        rows.append(
            {
                "neg": len(NEG.findall(text)),
                "msgs": nmsgs,
                "ntools": len(t),
                "viol": V.violations(t),
                "mx": max(ctx) if ctx else 0,
                "frust": bool(NEG.search(text)),
            }
        )
    n = len(rows)
    print(f"cc sessions: {n}   overall marker rate: {1000 * rate(rows):.1f} per 1k operator msgs")

    def band(rows, key, bands):
        for lo, hi, lab in bands:
            b = [r for r in rows if lo <= r[key] < hi]
            if b:
                pres = 100 * sum(x["frust"] for x in b) // len(b)
                print(
                    f"  {lab:9} n={len(b):>3}  presence={pres:>3}%   rate={1000 * rate(b):>5.1f}/1k"
                )

    print("\nby SESSION SIZE (n tools) — presence (confounded) vs rate (normalized):")
    band(
        rows,
        "ntools",
        [(0, 20, "<20"), (20, 60, "20-60"), (60, 200, "60-200"), (200, 10**9, ">200")],
    )
    print("\nby MAX CONTEXT — does the rot signal survive normalization?:")
    band(rows, "mx", [(0, 40000, "<40k"), (40000, 150000, "40-150k"), (150000, 10**9, ">150k")])

    print("\npredicate rate-lift (markers/msg with vs without):")
    for p in ["heredoc", "code_in_heredoc", "shell_file_write", "large_bash", "edit_loop"]:
        w = [r for r in rows if p in r["viol"]]
        wo = [r for r in rows if p not in r["viol"]]
        if not w or not wo:
            continue
        rw, rwo = rate(w), rate(wo)
        lift = (rw / rwo) if rwo else float("inf")
        print(
            f"  {p:18} rate|with={1000 * rw:>5.1f}/1k  rate|without={1000 * rwo:>5.1f}/1k  lift={lift:>4.1f}x"
        )


if __name__ == "__main__":
    main()
