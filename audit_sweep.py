#!/usr/bin/env python3
"""Sweep `mu audit` (deterministic process-layer auditors) over mu session event logs
and produce the findings TSV the dashboard reads — INCREMENTALLY.

`mu audit` is a Rust subprocess shelled once per session log; re-auditing every log
every run is what made the 15-min refresh overrun its window (~1100s over thousands of
logs). Almost none of those logs change between runs, so we cache per-file findings +
the file mtime in data/audit.sqlite (same pattern as judge_store / marks_store) and only
re-audit files that are new or whose mtime advanced. The TSV output is regenerated in
full from the cache each run, so the dashboard contract is unchanged — it's the `mu
audit` shell-outs, not the TSV write, that were expensive.

Steady state: audit only the day's changed logs (a handful) → seconds instead of ~18 min.
First run (cold cache): audits everything once, fanned out across the box's cores.

mu-only (cc has no `mu audit`). session_ref uses the canonical colon form
(mu:<daemon>:<session_id>) so it joins the degradation/scan output directly.

Run: ./run audit_sweep.py [out.tsv]   (default ~/mu-stats/mu-audit-findings.tsv)
"""

import concurrent.futures
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/mu-stats/mu-audit-findings.tsv")
EVENTS = os.path.expanduser("~/.local/share/mu/events/*/*.jsonl")
CACHE_DB = os.path.join(HERE, "data", "audit.sqlite")
FINDING = re.compile(r"\[(High|Medium|Low)\] (\w+) @event (\d+): (.*)")
# Resolve `mu` absolutely: cron runs with a minimal PATH that omits ~/.local/bin,
# so a bare "mu" raised FileNotFoundError. which() honors PATH; fall back to the
# known install location.
MU = shutil.which("mu") or os.path.expanduser("~/.local/bin/mu")

_DDL = """
CREATE TABLE IF NOT EXISTS audited(
    path        TEXT PRIMARY KEY,   -- the session event-log path we audited
    src_mtime   REAL,               -- its mtime at audit time (the watermark)
    audited_at  REAL                -- wall-clock epoch we audited it
);
CREATE TABLE IF NOT EXISTS findings(
    path        TEXT,               -- which log this finding came from (re-audit key)
    session_ref TEXT,               -- mu:<daemon>:<session_id>
    first_ts    TEXT,
    severity    TEXT,
    invariant   TEXT,
    event_id    TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS findings_path ON findings(path);
"""


def _ensure():
    os.makedirs(os.path.dirname(CACHE_DB), exist_ok=True)
    con = sqlite3.connect(CACHE_DB)
    con.executescript(_DDL)
    con.commit()
    return con


def first_ts(path):
    try:
        with open(path, errors="replace") as fh:
            ms = json.loads(fh.readline()).get("timestamp_unix_ms")
        if ms:
            return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()
    except (OSError, ValueError):
        pass
    return ""


def audit_file(path):
    """Return TSV-safe finding rows for one event log (or [] if none / audit fails)."""
    proc = subprocess.run([MU, "audit", path], capture_output=True, text=True)
    found = FINDING.findall(proc.stdout)
    if not found:
        return []
    daemon, sid = path.split("/")[-2], os.path.basename(path)[:-6]
    ts = first_ts(path)
    return [
        (f"mu:{daemon}:{sid}", ts, sev, inv, ev, detail.replace("\t", " "))
        for sev, inv, ev, detail in found
    ]


def audit_workers(n_files):
    """Bound the subprocess fan-out. The box has plenty of cores and `mu audit` is a
    short subprocess (threads release the GIL waiting on it), so scale with the CPU
    count — capped so a cold full sweep doesn't stampede the machine. Override with
    MU_ANALYTICS_AUDIT_WORKERS."""
    override = os.environ.get("MU_ANALYTICS_AUDIT_WORKERS")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    return max(1, min(24, n_files, (os.cpu_count() or 4)))


def enumerate_logs(events_glob=EVENTS):
    """{path: mtime} for every mu session event log currently on disk."""
    return {p: os.stat(p).st_mtime for p in glob.glob(events_glob)}


def select_changed(current, ledger):
    """current/ledger are {path: mtime}. Returns (to_audit, removed):
    to_audit = logs never audited or whose mtime advanced since we last did;
    removed  = logs we have cached findings for that no longer exist on disk."""
    to_audit = sorted(p for p, mt in current.items() if p not in ledger or mt > ledger[p])
    removed = [p for p in ledger if p not in current]
    return to_audit, removed


def _record(con, path, mtime, rows):
    """Replace one file's cached findings + bump its ledger watermark."""
    con.execute("DELETE FROM findings WHERE path = ?", [path])
    con.executemany(
        "INSERT INTO findings(path, session_ref, first_ts, severity, invariant, event_id, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        [(path, *r) for r in rows],
    )
    con.execute(
        "INSERT OR REPLACE INTO audited(path, src_mtime, audited_at) VALUES (?,?,?)",
        [path, mtime, time.time()],
    )


def _write_tsv(con, out):
    """Regenerate the full findings TSV from the cache (deterministic order), so the
    dashboard reads exactly what it did before — cached + freshly-audited together."""
    rows = con.execute(
        "SELECT session_ref, first_ts, severity, invariant, event_id, detail "
        "FROM findings ORDER BY path, CAST(event_id AS INTEGER)"
    ).fetchall()
    with open(out, "w") as fh:
        fh.write("session_ref\tfirst_ts\tseverity\tinvariant\tevent_id\tdetail\n")
        for r in rows:
            fh.write("\t".join(r) + "\n")
    return len(rows)


def main():
    con = _ensure()
    current = enumerate_logs()
    ledger = {p: mt for p, mt in con.execute("SELECT path, src_mtime FROM audited")}
    to_audit, removed = select_changed(current, ledger)

    for p in removed:
        con.execute("DELETE FROM findings WHERE path = ?", [p])
        con.execute("DELETE FROM audited WHERE path = ?", [p])
    if removed:
        con.commit()

    if to_audit:
        workers = audit_workers(len(to_audit))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for path, rows in zip(to_audit, ex.map(audit_file, to_audit), strict=True):
                _record(con, path, current[path], rows)
        con.commit()

    n = _write_tsv(con, OUT)
    con.close()
    print(
        f"{n} findings over {len(current)} logs -> {OUT}  "
        f"(audited {len(to_audit)} changed, skipped {len(current) - len(to_audit)}, "
        f"dropped {len(removed)})"
    )


if __name__ == "__main__":
    main()
