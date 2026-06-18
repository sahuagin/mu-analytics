#!/usr/bin/env python3
"""Operator-marks store — dashboard-owned, read + write.

READ unions two sources into the dashboard's `marks` slice:
  - the real `operator_mark` events already in mu's event log (source='mu_event'),
  - marks written through the dashboard (source='dashboard') in data/marks.sqlite.

WRITE: the static page can't POST, so the Sessions widget saves to localStorage and
exports a JSONL the operator drops into data/marks_inbox/; `ingest_inbox()` (called
from refresh.sh) folds those into marks.sqlite. The `synced` column lets a later job
replay dashboard marks back into mu as `operator_mark` events.

Run:  ./run marks_store.py    # prints the unioned marks
"""

import json
import os
import sqlite3
import time

from sample_data import _day

HERE = os.path.dirname(os.path.abspath(__file__))
MARKS_DB = os.path.join(HERE, "data", "marks.sqlite")
INBOX = os.path.join(HERE, "data", "marks_inbox")

_DDL = """
CREATE TABLE IF NOT EXISTS marks(
    task_id            TEXT,
    session_id         TEXT,
    daemon             TEXT,
    session_ref        TEXT,
    rating             INTEGER,
    note               TEXT,
    created_at_unix_ms INTEGER,
    source             TEXT DEFAULT 'dashboard',
    synced             INTEGER DEFAULT 0,
    PRIMARY KEY (task_id, created_at_unix_ms)
)
"""


def _ensure():
    os.makedirs(os.path.dirname(MARKS_DB), exist_ok=True)
    con = sqlite3.connect(MARKS_DB)
    con.execute(_DDL)
    cols = {r[1] for r in con.execute("PRAGMA table_info(marks)")}
    if "session_ref" not in cols:
        con.execute("ALTER TABLE marks ADD COLUMN session_ref TEXT")
    con.commit()
    return con


def _coerce_rating(r):
    try:
        return max(1, min(5, int(float(r))))
    except (TypeError, ValueError):
        return 3


def read_marks(ev_con):
    """Union event-log operator marks + dashboard marks -> [{date,rating,note,source}]."""
    out = []
    rows = ev_con.execute(
        "SELECT fleet || ':' || session, ts, json_extract_string(payload,'$.rating'), "
        "json_extract_string(payload,'$.note') "
        "FROM ev WHERE kind='operator_mark' ORDER BY ts"
    ).fetchall()
    for ref, ts, rating, note in rows:
        out.append(
            {
                "date": _day(ts),
                "session_ref": ref,
                "rating": _coerce_rating(rating),
                "note": note or "",
                "source": "mu_event",
            }
        )
    con = _ensure()
    for ts, ref, rating, note in con.execute(
        "SELECT created_at_unix_ms, session_ref, rating, note FROM marks ORDER BY created_at_unix_ms"
    ):
        out.append(
            {
                "date": _day(ts),
                "session_ref": ref or "",
                "rating": _coerce_rating(rating),
                "note": note or "",
                "source": "dashboard",
            }
        )
    con.close()
    out.sort(key=lambda m: m["date"])
    return out


def _session_ref(session_id=None, daemon=None, session_ref=None):
    if session_ref:
        return session_ref
    if session_id:
        return f"mu:{daemon}:{session_id}" if daemon else f"cc:{session_id}"
    return None


def read_marks_by_session(ev_con):
    """Latest operator mark per canonical session -> {session_ref: {rating, note, source}}.

    Same two sources read_marks() unions, but keyed by SESSION not date — the shape
    anomaly_worklist needs to flag/exclude already-labeled sessions:
      - operator_mark events in the ev view: an operator_mark lives in the MARKED
        session's own event stream, so its session_ref is fleet:session directly —
        the linkage is structural (the DuckDB-native, end-state source);
      - dashboard marks (data/marks.sqlite): keyed by (daemon, session_id) ->
        mu:<daemon>:<session_id>, or cc:<session_id> when no daemon.
    session_ref matches features.session_features' key exactly, so it joins directly.

    A session can be re-marked; latest-by-timestamp wins (the operator's current
    judgment). Deterministic: both sources are read in ts order."""
    latest = {}  # session_ref -> (ts, {rating, note, source})

    def _offer(ref, ts, rating, note, source):
        if ref and (ref not in latest or ts >= latest[ref][0]):
            latest[ref] = (
                ts,
                {"rating": _coerce_rating(rating), "note": note or "", "source": source},
            )

    for ref, ts, rating, note in ev_con.execute(
        "SELECT fleet || ':' || session, ts, "
        "json_extract_string(payload, '$.rating'), json_extract_string(payload, '$.note') "
        "FROM ev WHERE kind = 'operator_mark' "
        "AND json_extract_string(payload, '$.rating') IS NOT NULL ORDER BY ts"
    ).fetchall():
        _offer(ref, ts, rating, note, "mu_event")

    con = _ensure()
    for sid, daemon, ref, rating, note, ts in con.execute(
        "SELECT session_id, daemon, session_ref, rating, note, created_at_unix_ms FROM marks ORDER BY created_at_unix_ms"
    ):
        _offer(_session_ref(sid, daemon, ref), ts, rating, note, "dashboard")
    con.close()

    return {ref: v for ref, (_ts, v) in latest.items()}


def add_mark(
    task_id,
    rating,
    note="",
    session_id=None,
    daemon=None,
    created_at_unix_ms=None,
    session_ref=None,
):
    """Insert/replace one dashboard mark."""
    con = _ensure()
    ts = created_at_unix_ms if created_at_unix_ms is not None else int(time.time() * 1000)
    con.execute(
        "INSERT OR REPLACE INTO marks(task_id, session_id, daemon, session_ref, rating, note, "
        "created_at_unix_ms, source, synced) VALUES (?,?,?,?,?,?,?, 'dashboard', 0)",
        [
            task_id,
            session_id,
            daemon,
            _session_ref(session_id, daemon, session_ref),
            _coerce_rating(rating),
            note,
            ts,
        ],
    )
    con.commit()
    con.close()


def ingest_inbox(inbox=None):
    """Fold exported-mark JSONL files (one JSON object per line) into marks.sqlite,
    then move each consumed file aside. Returns the count ingested. Safe to call
    when the inbox is absent/empty (refresh.sh runs it every cycle). `inbox` defaults
    to the module INBOX, read at call time (not def time, so it stays overridable)."""
    if inbox is None:
        inbox = INBOX
    if not os.path.isdir(inbox):
        return 0
    n = 0
    for name in sorted(os.listdir(inbox)):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(inbox, name)
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                add_mark(
                    m.get("task_id") or m.get("id") or f"mark-{int(time.time() * 1000)}",
                    m.get("rating", 3),
                    m.get("note", ""),
                    m.get("session_id"),
                    m.get("daemon"),
                    m.get("created_at_unix_ms"),
                    m.get("session_ref") or m.get("ref"),
                )
                n += 1
        os.rename(path, path + ".ingested")
    return n


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "ingest":
        print(f"marks ingested: {ingest_inbox()}")
    else:
        import engine

        marks = read_marks(engine.connect())
        print(f"{len(marks)} marks (event-log + dashboard):")
        for m in marks:
            print(f"  {m['date']}  ★{m['rating']}  [{m['source']}]  {m['note'][:54]}")
