#!/usr/bin/env python3
"""Per-session "receipts" for one day, across BOTH fleets (mu + cc).

Backs the `/evening` skill — "what did I actually do today" — from the
consolidated event archive that `engine.py` already unions across every machine
and both harnesses. This is a *query*, not a re-scan: it reuses
`engine.connect()`'s `ev` view (DuckDB over the JSONL archive), so it inherits
the typed parse, the mu+cc union, and the canonical per-session key for free.

Run on the analytics host (where the archive + duckdb live):

  ./run daily_receipts.py                  # today (local midnight -> now), both fleets
  ./run daily_receipts.py --date 2026-06-23
  ./run daily_receipts.py --days 7 --json  # last 7 local days
  ./run daily_receipts.py --json           # machine-readable, for the skill

`ev` carries no project/cwd column (it lives only in the file path), so a
session's *topic* comes from its first user message; repo attribution is left to
the skill's git/beads layer. Stamp results as-of `as_of` — the archive is live,
so identical code drifts run-to-run (engine.py "hard-won knowledge" #6).
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import engine

HERE = os.path.dirname(os.path.abspath(__file__))

# kinds that count as "this session was actually active in the window" — drop the
# noise kinds (provider_status_update, context_assembly, ...) so a session that
# only emitted housekeeping doesn't masquerade as work.
_ACTIVE_KINDS = ("user_message", "tool_call", "assistant_message_event", "task_telemetry")


def _local_midnight(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _window(args) -> tuple[int, int, str]:
    """Return (start_ms, end_ms, label) in local time."""
    now = datetime.now().astimezone()
    if args.date:
        start = _local_midnight(datetime.fromisoformat(args.date).astimezone())
        end = start + timedelta(days=1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000), args.date
    days = max(1, args.days)
    start = _local_midnight(now) - timedelta(days=days - 1)
    label = "today" if days == 1 else f"last {days}d"
    return int(start.timestamp() * 1000), int(now.timestamp() * 1000), label


def collect(con, start_ms: int, end_ms: int) -> list[dict]:
    p = [start_ms, end_ms]

    # Sessions with real activity in the window. Kept as a CTE so the heavy
    # per-session aggregates only touch sessions that did something today.
    active_sql = f"""
        WITH active AS (
            SELECT DISTINCT fleet, session
            FROM ev
            WHERE ts >= ? AND ts < ?
              AND kind IN {_ACTIVE_KINDS}
        ),
        win AS (  -- counts within the day window only
            SELECT e.fleet, e.session,
                   min(e.ts) AS first_ts, max(e.ts) AS last_ts,
                   count(*) FILTER (WHERE e.kind = 'user_message') AS turns,
                   count(*) FILTER (WHERE e.kind = 'tool_call')    AS tool_calls
            FROM ev e JOIN active a USING (fleet, session)
            WHERE e.ts >= ? AND e.ts < ?
            GROUP BY 1, 2
        ),
        meta AS (  -- model/telemetry over the session's WHOLE life (handles
                   -- sessions that span local midnight: telemetry is emitted at end)
            SELECT ev.fleet, ev.session,
                   max(json_extract_string(payload, '$.model'))        AS model,
                   max(json_extract_string(payload, '$.provider_kind')) AS provider_kind,
                   max(json_extract_string(payload, '$.exit_reason'))   AS exit_reason,
                   max(TRY_CAST(json_extract_string(payload, '$.completion_tokens') AS BIGINT)) AS completion_tokens,
                   max(TRY_CAST(json_extract_string(payload, '$.prompt_tokens')     AS BIGINT)) AS prompt_tokens,
                   max(TRY_CAST(json_extract_string(payload, '$.cache_read_tokens') AS BIGINT)) AS cache_read_tokens
            FROM ev JOIN active USING (fleet, session)
            GROUP BY 1, 2
        )
        SELECT win.fleet, win.session, win.first_ts, win.last_ts, win.turns, win.tool_calls,
               meta.model, meta.provider_kind, meta.exit_reason,
               meta.completion_tokens, meta.prompt_tokens, meta.cache_read_tokens
        FROM win JOIN meta USING (fleet, session)
        ORDER BY win.first_ts
    """
    rows = con.execute(active_sql, p + p).fetchall()

    # Per-tool-name breakdown (in-window) and first-message topic — folded in by key.
    tools: dict[tuple, dict] = {}
    for fleet, session, tool, n in con.execute(
        """SELECT fleet, session, json_extract_string(payload, '$.name') AS tool, count(*) n
           FROM ev WHERE ts >= ? AND ts < ? AND kind = 'tool_call'
           GROUP BY 1, 2, 3""",
        p,
    ).fetchall():
        tools.setdefault((fleet, session), {})[tool or "?"] = n

    topics: dict[tuple, str] = {}
    for fleet, session, content in con.execute(
        """SELECT fleet, session, json_extract_string(payload, '$.content') AS content
           FROM ev WHERE ts >= ? AND ts < ? AND kind = 'user_message'
           QUALIFY row_number() OVER (PARTITION BY fleet, session ORDER BY ts ASC) = 1""",
        p,
    ).fetchall():
        topics[(fleet, session)] = (content or "").strip().replace("\n", " ")[:200]

    out = []
    for (
        fleet,
        session,
        first_ts,
        last_ts,
        turns,
        tool_calls,
        model,
        provider_kind,
        exit_reason,
        completion_tokens,
        prompt_tokens,
        cache_read_tokens,
    ) in rows:
        key = (fleet, session)
        out.append(
            {
                "fleet": fleet,
                "session": session,
                "start": datetime.fromtimestamp(first_ts / 1000)
                .astimezone()
                .isoformat(timespec="minutes"),
                "end": datetime.fromtimestamp(last_ts / 1000)
                .astimezone()
                .isoformat(timespec="minutes"),
                "duration_min": round((last_ts - first_ts) / 60000, 1),
                "turns": turns,
                "tool_calls": tool_calls,
                "tools": dict(sorted(tools.get(key, {}).items(), key=lambda kv: -kv[1])),
                "model": model,
                "provider_kind": provider_kind,
                "exit_reason": exit_reason,
                "tokens": {
                    "prompt": prompt_tokens,
                    "completion": completion_tokens,
                    "cache_read": cache_read_tokens,
                },
                "topic": topics.get(key, ""),
            }
        )
    return out


def summarize(sessions: list[dict], start_ms: int, end_ms: int, label: str) -> dict:
    by_fleet: dict[str, int] = {}
    for s in sessions:
        by_fleet[s["fleet"]] = by_fleet.get(s["fleet"], 0) + 1
    return {
        "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
        "window": {"label": label, "start_ms": start_ms, "end_ms": end_ms},
        "totals": {
            "sessions": len(sessions),
            "by_fleet": by_fleet,
            "tool_calls": sum(s["tool_calls"] for s in sessions),
            "turns": sum(s["turns"] for s in sessions),
        },
        "sessions": sessions,
    }


def print_human(report: dict) -> None:
    w = report["window"]
    t = report["totals"]
    fleets = " ".join(f"{k}={v}" for k, v in t["by_fleet"].items())
    print(f"# receipts — {w['label']}  (as of {report['as_of']})")
    print(
        f"  {t['sessions']} sessions ({fleets})   {t['turns']} turns   {t['tool_calls']} tool calls\n"
    )
    for s in report["sessions"]:
        top = ", ".join(f"{k}×{v}" for k, v in list(s["tools"].items())[:4]) or "—"
        clock = s["start"][11:] + "–" + s["end"][11:]
        print(
            f"  [{s['fleet']}] {clock}  {s['model'] or '?'}  "
            f"{s['turns']}t/{s['tool_calls']}tc  ({s['duration_min']}m)"
        )
        print(f"       tools: {top}")
        if s["topic"]:
            print(f"       topic: {s['topic']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Per-session daily receipts across mu + cc fleets.")
    ap.add_argument("--date", help="a specific local day, YYYY-MM-DD (default: today)")
    ap.add_argument(
        "--days", type=int, default=1, help="look back N local days from now (default 1)"
    )
    ap.add_argument("--json", action="store_true", help="emit JSON (for the /evening skill)")
    args = ap.parse_args()

    start_ms, end_ms, label = _window(args)
    con = engine.connect()
    sessions = collect(con, start_ms, end_ms)
    report = summarize(sessions, start_ms, end_ms, label)

    if args.json:
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print_human(report)


if __name__ == "__main__":
    main()
