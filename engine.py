#!/usr/bin/env python3
"""DuckDB query layer over mu's raw event-log JSONL.

DuckDB reads the event JSONL only. Its `sqlite_scanner` is a separately
distributed prebuilt extension with no freebsd_amd64 build (404 on auto-install),
so the SQLite sinks stay on stdlib `sqlite3` in sample_data.py. DuckDB's core
JSON reader is built in, which is all we need here.

The `payload` column MUST be pinned to JSON via explicit `columns=` — otherwise
read_json(union_by_name=true) collapses it to a MAP that exposes only `kind`, and
inner-key access (json_extract(payload,'$.token_breakdown')) returns NULL.

v1 reads mu events only (the rich behavioral/internal-ops signals are mu-native;
cc emits only tool_call + task_telemetry). cc cost/sessions come from the sink via
sample_data._load(), unchanged. cc event-log parity is a later phase.

Run:  ./run engine.py        # smoke: prints the per-kind histogram
"""

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

# Pin the schema so `payload` is JSON (not a kind-only MAP). Unlisted fields are
# ignored; ignore_errors skips malformed lines.
_COLUMNS = (
    "{'id':'BIGINT','session_id':'VARCHAR','timestamp_unix_ms':'BIGINT',"
    "'actor':'JSON','payload':'JSON'}"
)


def events_present() -> bool:
    """True if the mu event dir exists and has at least one log (gates the guard)."""
    if not os.path.isdir(MU_EVENTS):
        return False
    with os.scandir(MU_EVENTS) as it:
        for d in it:
            if d.is_dir():
                with os.scandir(d.path) as inner:
                    if any(f.name.endswith(".jsonl") for f in inner):
                        return True
    return False


def connect(glob: str = MU_GLOB, fleet: str = "mu") -> duckdb.DuckDBPyConnection:
    """Open a connection with the `ev` view registered over the event log.

    Columns: id, session_id, ts, daemon (the per-session dir name = the real
    session key, since session_id itself is not unique), kind, payload (JSON), fleet.
    """
    con = duckdb.connect()
    con.execute(
        f"""
        CREATE OR REPLACE VIEW ev AS
        SELECT
            id,
            session_id,
            timestamp_unix_ms AS ts,
            regexp_replace(filename, '.*/([^/]+)/[^/]+$', '\\1') AS daemon,
            json_extract_string(payload, '$.kind') AS kind,
            payload,
            '{fleet}' AS fleet
        FROM read_json(
            '{glob}',
            union_by_name = true,
            ignore_errors = true,
            filename = true,
            columns = {_COLUMNS}
        )
        """
    )
    return con


def histogram(con) -> list[tuple[str, int]]:
    return con.execute(
        "SELECT kind, count(*) AS n FROM ev GROUP BY kind ORDER BY n DESC"
    ).fetchall()


def smoke() -> None:
    if not events_present():
        print(f"NO EVENTS at {MU_EVENTS}")
        return
    con = connect()
    total = (con.execute("SELECT count(*) FROM ev").fetchone() or (0,))[0]
    daemons = (con.execute("SELECT count(DISTINCT daemon) FROM ev").fetchone() or (0,))[0]
    print(f"event-log rows: {total:,}   distinct daemons: {daemons:,}")
    print("per-kind histogram:")
    for kind, n in histogram(con):
        print(f"  {kind or '<null>':34} {n:>7,}")


if __name__ == "__main__":
    smoke()
