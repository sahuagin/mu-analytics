#!/usr/bin/env python3
"""Behavior-judge result store + processed-files ledger (sqlite, judge-owned).

The judge is expensive (one LLM call per (session, class), ~20s warm / ~200s cold
on the ollama box), so we must not re-judge a transcript that hasn't changed. This
is the standard file-ingest pattern: remember each file's modification time at the
moment we processed it; on the next run, only touch files whose mtime advanced past
what we recorded (or that we've never seen).

DuckDB (engine.py) is NOT the home for this — it opens in-memory and rebuilds its
view from the JSONL every run (engine.py:148), so it persists nothing. The durable
store on this project is sqlite (the sinks, data/marks.sqlite). This mirrors
marks_store.py: data/judge.sqlite, CREATE TABLE IF NOT EXISTS, upsert on a stable
key, read back for the dashboard.

Two tables:
  processed  — the ledger. One row per session/file: the source mtime we judged at
               (src_mtime) and when we judged it (processed_at). The incremental
               runner diffs current file mtime against src_mtime to find new work.
  verdicts   — the payload the dashboard reads. One row per (session_ref, behavior),
               INSERT OR REPLACE so a re-judge of a grown session overwrites cleanly
               instead of accumulating stale duplicates (the append-only JSONL bug).

Run:  ./run judge_store.py            # print a summary of what's been judged
"""

import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
JUDGE_DB = os.path.join(HERE, "data", "judge.sqlite")

_DDL = """
CREATE TABLE IF NOT EXISTS processed(
    session_ref   TEXT PRIMARY KEY,   -- cc:<uuid> | mu:<daemon>:<session>
    fleet         TEXT,               -- 'cc' | 'mu'
    src_mtime     REAL,               -- transcript file mtime AT judge time (the watermark)
    processed_at  REAL,               -- wall-clock epoch we judged it (provenance)
    n_verdicts    INTEGER             -- how many class verdicts we stored for it
);
CREATE TABLE IF NOT EXISTS verdicts(
    session_ref   TEXT,
    fleet         TEXT,
    behavior      TEXT,               -- false_success | map_as_terrain | ...
    occurred      INTEGER,            -- 1 / 0 / NULL (judge couldn't decide)
    severity      TEXT,
    confidence    REAL,
    n_evidence    INTEGER,
    src_mtime     REAL,               -- mtime this verdict was judged at
    judged_at     REAL,
    PRIMARY KEY (session_ref, behavior)
);
"""


def _ensure():
    os.makedirs(os.path.dirname(JUDGE_DB), exist_ok=True)
    con = sqlite3.connect(JUDGE_DB)
    con.executescript(_DDL)
    con.commit()
    return con


def processed_mtimes():
    """{session_ref: src_mtime} — the watermark the incremental selector diffs against.

    A session absent from this map has never been judged (process it); a session
    whose current file mtime exceeds the stored value has grown since (re-judge it)."""
    con = _ensure()
    out = {ref: mt for ref, mt in con.execute("SELECT session_ref, src_mtime FROM processed")}
    con.close()
    return out


def record(session_ref, fleet, src_mtime, verdicts, processed_at=None):
    """Persist one fully-judged session: upsert its ledger row + replace its verdicts.

    `verdicts` is the list of per-class result dicts run_judge_batch emits
    (keys: behavior, occurred, severity, confidence, n_evidence). Call this ONLY when
    the session judged cleanly across every class — a partially-failed session is left
    out of the ledger so it retries in full next run rather than being marked done."""
    now = processed_at if processed_at is not None else time.time()
    con = _ensure()
    con.execute(
        "INSERT OR REPLACE INTO processed(session_ref, fleet, src_mtime, processed_at, n_verdicts) "
        "VALUES (?,?,?,?,?)",
        [session_ref, fleet, src_mtime, now, len(verdicts)],
    )
    for v in verdicts:
        con.execute(
            "INSERT OR REPLACE INTO verdicts(session_ref, fleet, behavior, occurred, severity, "
            "confidence, n_evidence, src_mtime, judged_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                session_ref,
                fleet,
                v.get("behavior"),
                v.get("occurred"),
                v.get("severity"),
                v.get("confidence"),
                v.get("n_evidence"),
                src_mtime,
                now,
            ],
        )
    con.commit()
    con.close()


def read_verdicts(only_occurred=False):
    """All stored verdicts as dicts — the dashboard's read surface (wiring is a later task).

    only_occurred=True returns just the firing ones (occurred=1), the attention-queue cut."""
    con = _ensure()
    sql = (
        "SELECT session_ref, fleet, behavior, occurred, severity, confidence, n_evidence, "
        "src_mtime, judged_at FROM verdicts"
    )
    if only_occurred:
        sql += " WHERE occurred = 1"
    cols = [
        "session_ref",
        "fleet",
        "behavior",
        "occurred",
        "severity",
        "confidence",
        "n_evidence",
        "src_mtime",
        "judged_at",
    ]
    out = [dict(zip(cols, row, strict=True)) for row in con.execute(sql)]
    con.close()
    return out


if __name__ == "__main__":
    con = _ensure()
    n_proc = con.execute("SELECT count(*) FROM processed").fetchone()[0]
    n_verd = con.execute("SELECT count(*) FROM verdicts").fetchone()[0]
    n_fired = con.execute("SELECT count(*) FROM verdicts WHERE occurred = 1").fetchone()[0]
    by_fleet = con.execute(
        "SELECT fleet, count(*) FROM processed GROUP BY fleet ORDER BY fleet"
    ).fetchall()
    con.close()
    print(f"judge store: {JUDGE_DB}")
    print(f"  processed sessions: {n_proc}  ({'  '.join(f'{f}={n}' for f, n in by_fleet) or '-'})")
    print(f"  verdict rows: {n_verd}   occurred=1: {n_fired}")
