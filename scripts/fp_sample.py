#!/usr/bin/env python3
"""FP-negative sample over the ev view (the FP denominator for behavior-judge
validation), plus a presence-check of the incident POSITIVES.

Negatives = sessions presumed-clean: no operator_mark, >=2 tool calls, NOT faux,
NOT a research meta-session, NOT an incident positive. Stratified by
fleet x tool-call band, ordered by md5(session) so independent runs get the SAME
deterministic set.

The incident POSITIVES (the labeled positive set) and the research META_SESSIONS
are study-specific and PRIVATE — they live in config.toml's [adherence] section
(gitignored; see config.example.toml), never in this file. With an empty config
this still builds the stratified FP-negative sample over all presumed-clean
sessions; the positive presence-check simply has nothing to report.

    cd <repo> && python3 scripts/fp_sample.py [out.tsv]
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine  # noqa: E402

con = engine.connect()

# Study-specific labeled sets — loaded from config (gitignored), empty by default.
_acfg = getattr(engine, "_cfg", {}).get("adherence", {})
POSITIVES = _acfg.get("positives", {})  # {session_ref: "behavior-class label"}
META_SESSIONS = _acfg.get("meta_sessions", [])  # [id substring to exclude, ...]

# exclusion substrings: research meta-sessions + every positive's id tail
EXCL = list(META_SESSIONS) + [ref.split(":", 1)[1] for ref in POSITIVES]
not_like = " AND ".join(f"session NOT LIKE '%{s}%'" for s in EXCL) if EXCL else "TRUE"

SQL = f"""
WITH agg AS (
  SELECT session, any_value(fleet) AS fleet,
         count(*) FILTER (WHERE kind='tool_call') AS tool_calls,
         min(ts) AS started,
         max(CASE WHEN kind='operator_mark' THEN 1 ELSE 0 END) AS has_mark
  FROM ev GROUP BY session
),
faux AS (
  SELECT DISTINCT session FROM ev WHERE fleet='mu' AND kind='task_telemetry'
  AND json_extract_string(payload,'$.model')='faux'
),
elig AS (
  SELECT * FROM agg
  WHERE has_mark=0 AND tool_calls>=2
    AND session NOT IN (SELECT session FROM faux)
    AND {not_like}
),
banded AS (
  SELECT *, CASE WHEN tool_calls<20 THEN 'S' WHEN tool_calls<80 THEN 'M' ELSE 'L' END AS band
  FROM elig
),
ranked AS (
  SELECT *, row_number() OVER (PARTITION BY fleet, band ORDER BY md5(session)) AS rn
  FROM banded
)
SELECT fleet, session, band, tool_calls, started FROM ranked WHERE rn <= 20
ORDER BY fleet, band, rn
"""

rows = con.execute(SQL).fetchall()
out = sys.argv[1] if len(sys.argv) > 1 else "/dev/stdout"
with open(out, "w") as fh:
    fh.write("session_ref\tfleet\tband\ttool_calls\tstarted_at\n")
    for fleet, session, band, tc, started in rows:
        ts = (
            datetime.datetime.fromtimestamp(started / 1000, tz=datetime.UTC).isoformat()
            if started
            else ""
        )
        fh.write(f"{fleet}:{session}\t{fleet}\t{band}\t{tc}\t{ts}\n")

from collections import Counter  # noqa: E402

c = Counter((r[0], r[2]) for r in rows)
print(f"FP negative sample: {len(rows)} sessions -> {out}")
for (fleet, band), n in sorted(c.items()):
    print(f"  {fleet} {band}: {n}")

if POSITIVES:
    print("\nPositive presence in ev:")
    present = {r[0] for r in con.execute("SELECT DISTINCT session FROM ev").fetchall()}
    for ref, label in POSITIVES.items():
        mark = "PRESENT" if ref.split(":", 1)[1] in present else "absent"
        print(f"  [{mark:7}] {ref}  — {label}")
