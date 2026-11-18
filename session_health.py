#!/usr/bin/env python3
"""Per-session health of the mu archive, and the recover worklist.

Each session log is diagnosed by mu-core itself (mu_events_py.diagnose_session,
the same reads `mu recover` makes): clean, torn tail, ragged end (died mid-turn),
gap (missing lines), no conversation, or a legacy head that needs its
predecessor. Logs are read one at a time, in Rust, with no model and no DuckDB.
Rows are cached in data/session_health.jsonl keyed by file size and mtime, so a
refresh only re-reads logs that changed.

A row holds ids, counts, statuses, open call names and error strings, never
message text, tool arguments or tool output.

Analytics only diagnoses. Recovery is a daemon action on the machine that owns
the log: the worklist names the `mu recover` command to run there, and gaps are
listed for inspection, not recovery.

Run:  ./run session_health.py [out.md]   (default ~/mu-stats/recover-worklist.md)
"""

import json
import os
import sys
import time
from glob import glob
from pathlib import Path

import engine

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    import mu_events_py as _EV
except Exception:
    _EV = None

CACHE = Path(engine.HERE) / "data" / "session_health.jsonl"
# A ragged log modified this recently may be a live session mid-turn.
LIVE_HOURS = 24
# Statuses that need a person to look, not a recover command.
INSPECT = ("gap", "bad_predecessor", "unrecoverable")


def _diagnose(path):
    if _EV is None or not hasattr(_EV, "diagnose_session"):
        sys.exit(
            "session_health: mu_events_py.diagnose_session missing — run `just build-events-parser`"
        )
    return json.loads(_EV.diagnose_session(path))


def _location(path):
    """(machine, daemon) from <root>/<machine>/events/<daemon>/session-N.jsonl,
    or ("local", daemon) for the legacy single-machine layout or a log outside
    the archive."""
    p = Path(path)
    daemon = p.parent.name
    if engine._MU_EVENTS_ROOT and p.is_relative_to(engine.MU_EVENTS):
        return p.relative_to(engine.MU_EVENTS).parts[0], daemon
    return "local", daemon


def _load_cache(cache):
    rows = {}
    if cache.exists():
        for line in cache.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            # a row missing its key fields is re-diagnosed, not fatal
            if isinstance(row, dict) and all(k in row for k in ("file", "bytes", "mtime_ns")):
                rows[row["file"]] = row
    return rows


def refresh(files=None, cache=CACHE, diagnose=_diagnose):
    """Diagnose every log in `files` (default: the mu archive glob), reusing
    cached rows for logs whose size and mtime are unchanged. Returns the rows."""
    if files is None:
        files = sorted(glob(engine.MU_GLOB))
    cached = _load_cache(cache)
    rows, fresh, unreadable = [], 0, 0
    for path in files:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue
        row = cached.get(path)
        if row is None or (row["bytes"], row["mtime_ns"]) != (st.st_size, st.st_mtime_ns):
            try:
                row = diagnose(path)
            except FileNotFoundError:
                continue
            except OSError as e:
                # one unreadable log (permissions, I/O) must not stop the run
                print(f"session_health: cannot read {path}: {e}", file=sys.stderr)
                unreadable += 1
                continue
            machine, daemon = _location(path)
            session = row.get("session_id") or Path(path).stem
            row.update(
                file=path,
                machine=machine,
                daemon=daemon,
                session_ref=f"{daemon}:{session}",
                bytes=st.st_size,
                mtime_ns=st.st_mtime_ns,
            )
            fresh += 1
        rows.append(row)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(cache)
    print(
        f"session_health: {len(rows)} logs, {fresh} diagnosed, {len(rows) - fresh} cached, "
        f"{unreadable} unreadable"
    )
    return rows


def _age_h(row, now):
    return (now - row["mtime_ns"] / 1e9) / 3600


def worklist(rows, now=None, live_hours=LIVE_HOURS):
    """The recover worklist as markdown."""
    now = time.time() if now is None else now
    by = {}
    for r in rows:
        by.setdefault(r["status"], []).append(r)
    ragged = sorted(by.get("ragged", []), key=lambda r: r["mtime_ns"])
    live = [r for r in ragged if _age_h(r, now) < live_hours]
    ragged = [r for r in ragged if _age_h(r, now) >= live_hours]

    counts = ", ".join(f"{k} {len(v):,}" for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])))
    out = [
        "# mu recover worklist",
        "",
        f"{len(rows):,} session logs: {counts}.",
        "",
        "Run each command on the named machine. Without `--apply`, `mu recover` only reports",
        "where the new head would fork; add `--apply` to start it. The damaged log is never",
        "written. Use `--close-calls` when the open call is what keeps killing the session.",
        "",
        f"## Ragged ({len(ragged)})",
        "",
    ]
    if ragged:
        out += [
            "| machine | session | open calls | fork | age (d) | command |",
            "|---|---|---|---|---|---|",
        ]
        for r in ragged:
            calls = ", ".join(c["tool_name"] for c in r["open_calls"]) or "-"
            out.append(
                f"| {r['machine']} | `{r['session_ref']}` | {calls} | {r['fork_event_id']} "
                f"| {_age_h(r, now) / 24:.0f} | `mu recover {r['session_ref']}` |"
            )
    else:
        out.append("_None._")

    needs = by.get("needs_predecessor", [])
    out += ["", f"## Needs predecessor ({len(needs)})", ""]
    if needs:
        out += [
            "Legacy heads. The log names the predecessor session but not its daemon;",
            "find the predecessor's log on the same machine and pass it as `daemon:session`.",
            "",
            "| machine | session | predecessor | command |",
            "|---|---|---|---|",
        ]
        for r in needs:
            out.append(
                f"| {r['machine']} | `{r['session_ref']}` | `{r['predecessor_session_id']}` "
                f"| `mu recover {r['session_ref']} --predecessor <daemon>:{r['predecessor_session_id']}` |"
            )
    else:
        out.append("_None._")

    inspect = [r for s in INSPECT for r in by.get(s, [])]
    out += ["", f"## Inspect ({len(inspect)})", ""]
    if inspect:
        out += ["| machine | session | status | detail |", "|---|---|---|---|"]
        for r in inspect:
            detail = (r.get("detail") or "").replace("|", "\\|")
            out.append(f"| {r['machine']} | `{r['session_ref']}` | {r['status']} | {detail} |")
    else:
        out.append("_None._")

    out += ["", f"## Possibly live ({len(live)})", ""]
    if live:
        out.append(f"Ragged but modified in the last {live_hours} h; may still be running.")
        out.append("")
        out += [f"- {r['machine']} `{r['session_ref']}`" for r in live]
    else:
        out.append("_None._")
    return "\n".join(out) + "\n"


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "mu-stats/recover-worklist.md"
    rows = refresh()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(worklist(rows))
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
