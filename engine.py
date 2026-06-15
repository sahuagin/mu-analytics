#!/usr/bin/env python3
"""DuckDB query layer over the agent event-log JSONL (both fleets).

DuckDB reads the event JSONL only. Its `sqlite_scanner` is a separately
distributed prebuilt extension with no freebsd_amd64 build (404 on auto-install),
so the SQLite sinks stay on stdlib `sqlite3` in sample_data.py. DuckDB's core
JSON reader is built in, which is all we need here.

The `payload` column MUST be pinned to JSON via explicit `columns=` — otherwise
read_json(union_by_name=true) collapses it to a MAP that exposes only `kind`, and
inner-key access (json_extract(payload,'$.token_breakdown')) returns NULL.

The `ev` view unions BOTH fleets onto one schema (the mu-core SessionEvent
format): mu-native events and the full-fidelity claude-code stream emitted by
cc_telemetry.py (WS2). Each row is tagged `fleet` ('mu' | 'cc') so one query
surface feeds every panel for both fleets. cc previously emitted only
tool_call + task_telemetry; it now carries the rich behavioral kinds
(user_message / assistant_message_event / tool_result / done) too.

Run:  ./run engine.py        # smoke: prints the per-kind histogram per fleet
"""

import glob as _glob
import os
import tomllib

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
# config.toml is machine-specific (gitignored); fall back to the committed example
# so imports work in CI / fresh checkouts (tests use fixtures, not these paths).
_CFG_PATH = os.path.join(HERE, "config.toml")
if not os.path.exists(_CFG_PATH):
    _CFG_PATH = os.path.join(HERE, "config.example.toml")
_cfg = tomllib.load(open(_CFG_PATH, "rb"))
PATHS = _cfg["paths"]

# mu's event log sits beside its sink:  <...>/mu/telemetry.sqlite -> <...>/mu/events
MU_EVENTS = os.path.join(os.path.dirname(PATHS["mu_sink_db"]), "events")
MU_GLOB = os.path.join(MU_EVENTS, "*", "*.jsonl")

# cc's full-fidelity event log (the WS2 emitter's output): one JSONL per session
# under a provider dir, e.g.  <cc_events_out>/claude-code/<session-uuid>.jsonl
CC_EVENTS: str = PATHS.get("cc_events_out", "") or ""
CC_GLOB = os.path.join(CC_EVENTS, "*", "*.jsonl") if CC_EVENTS else ""

# `daemon` is the daemon PROCESS, not the session. A mu daemon dir hosts MULTIPLE
# session files (session-1.jsonl, session-2.jsonl, supervisor.jsonl), and the raw
# session_id is only a per-daemon counter ("session-1") that repeats across every
# daemon — so neither alone identifies a session.
_MU_DAEMON = r"regexp_replace(filename, '.*/([^/]+)/[^/]+$', '\1')"  # the daemon dir name
_CC_DAEMON = "session_id"  # cc has no daemon; one file == one session (the UUID)

# `session` is the canonical per-session key: daemon_id:session_id. That
# disambiguates session-1 from session-2 within a daemon (the legacy/M3 unit —
# panels.mu_sessions groups by (daemon, session_id)). cc is one session per file,
# so its session IS the unique session_id.
_MU_SESSION = _MU_DAEMON + " || ':' || session_id"
_CC_SESSION = "session_id"

# Pin the schema so `payload` is JSON (not a kind-only MAP). Unlisted fields are
# ignored; ignore_errors skips malformed lines.
_COLUMNS = (
    "{'id':'BIGINT','session_id':'VARCHAR','timestamp_unix_ms':'BIGINT',"
    "'actor':'JSON','payload':'JSON'}"
)

# A source = (glob, fleet, daemon_expr, session_expr). The default reads every fleet.
_DEFAULT_SOURCES = [(MU_GLOB, "mu", _MU_DAEMON, _MU_SESSION)]
if CC_GLOB:
    _DEFAULT_SOURCES.append((CC_GLOB, "cc", _CC_DAEMON, _CC_SESSION))


def _glob_has_files(pattern: str) -> bool:
    """True if the glob matches >=1 file (read_json errors on a no-match pattern)."""
    return bool(pattern) and bool(_glob.glob(pattern))


def _dir_has_jsonl(d: str) -> bool:
    if not d or not os.path.isdir(d):
        return False
    for _root, _dirs, files in os.walk(d):
        if any(f.endswith(".jsonl") for f in files):
            return True
    return False


def events_present() -> bool:
    """True if ANY fleet's event dir (mu or cc) has at least one log (gates smoke)."""
    return _dir_has_jsonl(MU_EVENTS) or _dir_has_jsonl(CC_EVENTS)


def _select_for(glob: str, fleet: str, daemon_expr: str, session_expr: str) -> str:
    return f"""
        SELECT
            id,
            session_id,
            timestamp_unix_ms AS ts,
            {daemon_expr} AS daemon,
            {session_expr} AS session,
            json_extract_string(payload, '$.kind') AS kind,
            payload,
            '{fleet}' AS fleet
        FROM read_json(
            '{glob}',
            union_by_name = true,
            ignore_errors = true,
            filename = true,
            columns = {_COLUMNS}
        )"""


def connect(
    glob: str | None = None,
    fleet: str = "mu",
    sources: list[tuple[str, ...]] | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open a connection with the `ev` view registered over the event log(s).

    Columns: id, session_id, ts, daemon (the daemon process),
    session (the canonical per-session key, daemon_id:session_id for mu),
    kind, payload (JSON), fleet ('mu' | 'cc'). Group analytics by `session`,
    not `daemon` — a mu daemon hosts multiple sessions.

    - Production (no args): UNION every present fleet (mu + cc), so one schema
      feeds every panel for both fleets. Empty/missing fleets are skipped.
    - `glob=`/`fleet=`: a single explicit source (the hermetic-fixture path used
      by tests), keyed mu-style (daemon dir + session_id).
    - `sources=`: explicit (glob, fleet, daemon_expr[, session_expr]) tuples;
      a 3-tuple defaults session_expr to daemon_expr.
    """
    if glob is not None:
        srcs: list[tuple[str, ...]] = [(glob, fleet, _MU_DAEMON, _MU_SESSION)]
    elif sources is not None:
        # accept 3-tuples (legacy) by defaulting session_expr to the daemon_expr
        srcs = [s if len(s) >= 4 else (s[0], s[1], s[2], s[2]) for s in sources]
    else:
        srcs = [s for s in _DEFAULT_SOURCES if _glob_has_files(s[0])]

    con = duckdb.connect()
    if not srcs:
        # No event logs present anywhere — register an empty, correctly-typed view
        # so callers can query `ev` without a crash (returns zero rows).
        con.execute(
            "CREATE OR REPLACE VIEW ev AS SELECT "
            "NULL::BIGINT AS id, NULL::VARCHAR AS session_id, NULL::BIGINT AS ts, "
            "NULL::VARCHAR AS daemon, NULL::VARCHAR AS session, NULL::VARCHAR AS kind, "
            "NULL::JSON AS payload, NULL::VARCHAR AS fleet WHERE false"
        )
        return con

    union = "\n        UNION ALL\n".join(_select_for(*s) for s in srcs)
    con.execute(f"CREATE OR REPLACE VIEW ev AS{union}")
    return con


def histogram(con) -> list[tuple[str, int]]:
    return con.execute(
        "SELECT kind, count(*) AS n FROM ev GROUP BY kind ORDER BY n DESC"
    ).fetchall()


def fleet_histogram(con) -> list[tuple[str, str, int]]:
    return con.execute(
        "SELECT fleet, kind, count(*) AS n FROM ev GROUP BY fleet, kind ORDER BY fleet, n DESC"
    ).fetchall()


def smoke() -> None:
    if not events_present():
        print(f"NO EVENTS (mu={MU_EVENTS!r}  cc={CC_EVENTS!r})")
        return
    con = connect()
    total = (con.execute("SELECT count(*) FROM ev").fetchone() or (0,))[0]
    daemons = (con.execute("SELECT count(DISTINCT daemon) FROM ev").fetchone() or (0,))[0]
    by_fleet = con.execute(
        "SELECT fleet, count(*) FROM ev GROUP BY fleet ORDER BY fleet"
    ).fetchall()
    fleets = "  ".join(f"{f}={n:,}" for f, n in by_fleet)
    print(f"event-log rows: {total:,}   distinct sessions: {daemons:,}   ({fleets})")
    print("per-fleet / per-kind histogram:")
    cur_fleet = None
    for fleet, kind, n in fleet_histogram(con):
        if fleet != cur_fleet:
            print(f"  [{fleet}]")
            cur_fleet = fleet
        print(f"    {kind or '<null>':32} {n:>7,}")


if __name__ == "__main__":
    smoke()
