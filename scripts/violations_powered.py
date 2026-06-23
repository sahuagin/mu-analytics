#!/usr/bin/env python3
"""Per-predicate violation PREVALENCE at power over the typed `ev` view — the
authoritative base rates (powered replacement for the raw-jsonl violations.py
prevalence). Reuses violations.violations() + its RX_* classifiers verbatim; only
the data layer changes (ev tool_call events, both fleets). mu model='faux' test
sessions excluded (round 8). % of tool-bearing sessions per fleet.

Run on the deployed host:  cd ~/src/public_github/mu-analytics &&
    PYTHONPATH=/tmp/adh-scripts:. python3 /tmp/adh-scripts/violations_powered.py
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402
import violations as V  # noqa: E402

con = engine.connect()

faux = {
    r[0]
    for r in con.execute(
        "SELECT DISTINCT session FROM ev WHERE fleet='mu' AND kind='task_telemetry' "
        "AND (json_extract_string(payload,'$.model')='faux' "
        "OR json_extract_string(payload,'$.provider_kind') IN ('faux','mock'))"
    ).fetchall()
}

# Ordered tool stream per session (name_lower, arguments-json-string). args via
# json_extract(...)::VARCHAR — json_extract_string returns NULL for an object.
streams = {}
for session, fleet, name, args in con.execute(
    "SELECT session, fleet, json_extract_string(payload,'$.name') AS name, "
    "json_extract(payload,'$.arguments')::VARCHAR AS args "
    "FROM ev WHERE kind='tool_call' ORDER BY session, id"
).fetchall():
    if fleet == "mu" and session in faux:
        continue
    streams.setdefault(session, (fleet, []))[1].append((str(name or "").lower(), args or ""))

PREDS = [
    "heredoc", "code_in_heredoc", "shell_file_write", "large_bash",
    "dangerous_bash", "force_push", "edit_loop", "edit_before_read",
]
ntool = defaultdict(int)
counts = {"cc": defaultdict(int), "mu": defaultdict(int)}
for _session, (fleet, tools) in streams.items():
    if not tools or fleet not in counts:
        continue
    ntool[fleet] += 1
    for p in V.violations(tools):
        counts[fleet][p] += 1

print(f"tool-bearing sessions (faux-excluded): cc={ntool['cc']:,}  mu={ntool['mu']:,}")
print(f"{'predicate':18} {'cc%':>6} {'mu%':>6}   (cc n / mu n)")
for p in PREDS:
    ccp = 100 * counts["cc"][p] / ntool["cc"] if ntool["cc"] else 0
    mup = 100 * counts["mu"][p] / ntool["mu"] if ntool["mu"] else 0
    print(f"{p:18} {ccp:>5.1f}% {mup:>5.1f}%   ({counts['cc'][p]} / {counts['mu'][p]})")
