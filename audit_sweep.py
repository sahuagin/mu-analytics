#!/usr/bin/env python3
"""Sweep `mu audit` (deterministic process-layer auditors) over every mu session
event log and record findings to a TSV — moved from claude-personal/scripts.

NOT a substrate consumer: the auditors live in the `mu` Rust binary; this shells
`mu audit` per session log and parses its findings. mu-only (cc has no `mu audit`).
Consumed by the dashboard's degradation section (audit findings table).

session_ref uses the canonical colon form (mu:<daemon>:<session_id>) so it joins
the degradation/scan output directly — the legacy emitted a slash form.

Run: ./run audit_sweep.py [out.tsv]   (default ~/mu-stats/mu-audit-findings.tsv)
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime

OUT = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/mu-stats/mu-audit-findings.tsv")
EVENTS = os.path.expanduser("~/.local/share/mu/events/*/*.jsonl")
FINDING = re.compile(r"\[(High|Medium|Low)\] (\w+) @event (\d+): (.*)")
# Resolve `mu` absolutely: cron runs with a minimal PATH that omits ~/.local/bin,
# so a bare "mu" raised FileNotFoundError. which() honors PATH; fall back to the
# known install location.
MU = shutil.which("mu") or os.path.expanduser("~/.local/bin/mu")


def first_ts(path):
    try:
        with open(path, errors="replace") as fh:
            ms = json.loads(fh.readline()).get("timestamp_unix_ms")
        if ms:
            return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()
    except (OSError, ValueError):
        pass
    return ""


def sweep(events_glob=EVENTS):
    """Yield (session_ref, first_ts, severity, invariant, event_id, detail) tuples."""
    for f in sorted(glob.glob(events_glob)):
        proc = subprocess.run([MU, "audit", f], capture_output=True, text=True)
        found = FINDING.findall(proc.stdout)
        if not found:
            continue
        daemon, sid = f.split("/")[-2], os.path.basename(f)[:-6]
        ts = first_ts(f)
        for sev, inv, ev, detail in found:
            yield (f"mu:{daemon}:{sid}", ts, sev, inv, ev, detail.replace("\t", " "))


def main():
    rows = list(sweep())
    scanned = len(glob.glob(EVENTS))
    with open(OUT, "w") as fh:
        fh.write("session_ref\tfirst_ts\tseverity\tinvariant\tevent_id\tdetail\n")
        for r in rows:
            fh.write("\t".join(r) + "\n")
    print(f"{len(rows)} findings across {scanned} session logs -> {OUT}")


if __name__ == "__main__":
    main()
