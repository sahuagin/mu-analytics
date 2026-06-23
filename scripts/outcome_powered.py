#!/usr/bin/env python3
"""Round-5 outcome re-test (violation/context × operator frustration) AT POWER,
over the typed `ev` view — the powered replacement for the raw-jsonl `outcome.py`.

Outcome = operator-frustration marker RATE (markers / operator message), which
controls for exposure (round 5: PRESENCE is a session-length artifact). Reuses
`outcome.NEG` (markers, verbatim from scans.py) and `violations.violations()` /
its RX_* classifiers unchanged — only the data layer changes (ev, not raw jsonl).

cc operator messages = `user_message` events with no `meta` flag (harness-injected
turns carry meta=true in cc_telemetry). Tool stream = `tool_call` (name+arguments).
Max context = `assistant_message_event.message.usage` (input+cache).

Run on the deployed host:  cd ~/src/public_github/mu-analytics && python3 scripts/outcome_powered.py
Caveat: cc bench can't be path-excluded in ev (UUID-keyed post-conversion); bench
sessions are scripted (≈0 operator markers) so they dilute toward zero, not up.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402
import outcome as O  # noqa: E402  (reuse NEG markers + rate)
import violations as V  # noqa: E402  (reuse violations() + RX_* classifiers)

CC_CTX = (
    "COALESCE(json_extract(payload,'$.message.usage.input_tokens')::BIGINT,0)"
    "+COALESCE(json_extract(payload,'$.message.usage.cache_read_input_tokens')::BIGINT,0)"
    "+COALESCE(json_extract(payload,'$.message.usage.cache_creation_input_tokens')::BIGINT,0)"
)


def build_rows(con):
    # Operator-typed text per cc session (exclude harness-injected meta turns).
    text = {}
    msgs = {}
    for s, c in con.execute(
        "SELECT session, json_extract_string(payload,'$.content') AS c "
        "FROM ev WHERE fleet='cc' AND kind='user_message' "
        "AND json_extract(payload,'$.meta') IS NULL"
    ).fetchall():
        if c and c.strip():
            text.setdefault(s, []).append(c)
            msgs[s] = msgs.get(s, 0) + 1

    # Ordered tool stream per session (name_lower, arguments-json-string).
    tools = {}
    for s, name, args in con.execute(
        "SELECT session, json_extract_string(payload,'$.name') AS name, "
        "json_extract_string(payload,'$.arguments') AS args "
        "FROM ev WHERE fleet='cc' AND kind='tool_call' ORDER BY session, id"
    ).fetchall():
        tools.setdefault(s, []).append((str(name or "").lower(), args or ""))

    # Max per-turn context per session.
    mx = dict(
        con.execute(
            f"SELECT session, max({CC_CTX}) FROM ev "
            "WHERE fleet='cc' AND kind='assistant_message_event' GROUP BY session"
        ).fetchall()
    )

    rows = []
    for s, n in msgs.items():
        if n == 0:
            continue
        t = tools.get(s, [])
        if not t:
            continue
        body = "\n".join(text.get(s, []))
        rows.append(
            {
                "neg": len(O.NEG.findall(body)),
                "msgs": n,
                "ntools": len(t),
                "viol": V.violations(t),
                "mx": int(mx.get(s) or 0),
                "frust": bool(O.NEG.search(body)),
            }
        )
    return rows


def band(rows, key, bands):
    for lo, hi, lab in bands:
        b = [r for r in rows if lo <= r[key] < hi]
        if b:
            pres = 100 * sum(x["frust"] for x in b) // len(b)
            print(f"  {lab:9} n={len(b):>4}  presence={pres:>3}%   rate={1000*O.rate(b):>6.1f}/1k")


def main():
    con = engine.connect()
    rows = build_rows(con)
    print(f"cc sessions (operator msgs + tools): {len(rows):,}   "
          f"overall marker rate: {1000*O.rate(rows):.1f} per 1k operator msgs")

    print("\nby SESSION SIZE (n tools) — presence (confounded) vs rate (normalized):")
    band(rows, "ntools", [(0, 20, "<20"), (20, 60, "20-60"), (60, 200, "60-200"), (200, 10**9, ">200")])

    print("\nby MAX CONTEXT — does the rot signal survive normalization? (H4):")
    band(rows, "mx", [(0, 40000, "<40k"), (40000, 150000, "40-150k"), (150000, 10**9, ">150k")])

    print("\npredicate rate-lift (markers/msg with vs without):")
    for p in ["heredoc", "code_in_heredoc", "shell_file_write", "large_bash", "edit_loop"]:
        w = [r for r in rows if p in r["viol"]]
        wo = [r for r in rows if p not in r["viol"]]
        if not w or not wo:
            continue
        rw, rwo = O.rate(w), O.rate(wo)
        lift = (rw / rwo) if rwo else float("inf")
        print(f"  {p:18} with={1000*rw:>6.1f}/1k (n={len(w):>4})  "
              f"without={1000*rwo:>6.1f}/1k  lift={lift:>4.1f}x")


if __name__ == "__main__":
    main()
