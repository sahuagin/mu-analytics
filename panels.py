#!/usr/bin/env python3
"""Per-page analytics slices derived from the event-log JSONL via DuckDB.

Each builder takes a connection with the `ev` view registered (see engine.py)
and returns JSON-able structures matching the shapes proto/index.html consumes.
Cost math reuses sample_data's rate table + formula — never re-derived here.

`./run panels.py` prints each slice (a verification harness).
"""
import engine

# Source labels for recall provenance -> the proto's display strings.
_SRC_LABEL = {
    "project_file": "ProjectFile", "filesystem": "ProjectFile",
    "memory": "Memory", "bootloader": "Bootloader",
}
_COMPACTION_ACTIONS = ("kept", "dropped", "summarized", "failed")


def tool_mix(con, limit=12):
    """tool_call name distribution -> [{tool, count}]."""
    rows = con.execute(
        """
        SELECT json_extract_string(payload,'$.name') AS tool, count(*) AS count
        FROM ev WHERE kind='tool_call' AND json_extract_string(payload,'$.name') IS NOT NULL
        GROUP BY 1 ORDER BY count DESC LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [{"tool": t, "count": int(c)} for t, c in rows]


def recall(con):
    """recall_provenance items grouped by source -> [{source, items, tokens}]."""
    rows = con.execute(
        """
        WITH it AS (
            SELECT unnest(json_extract(payload,'$.items')::JSON[]) AS item
            FROM ev WHERE kind='recall_provenance'
        )
        SELECT json_extract_string(item,'$.source') AS src,
               count(*) AS items,
               COALESCE(sum(json_extract(item,'$.token_count')::BIGINT),0) AS tokens
        FROM it GROUP BY 1 ORDER BY tokens DESC
        """
    ).fetchall()
    out = {}
    for src, items, tokens in rows:
        label = _SRC_LABEL.get(src, (src or "unknown").title())
        agg = out.setdefault(label, {"source": label, "items": 0, "tokens": 0})
        agg["items"] += int(items)
        agg["tokens"] += int(tokens)
    return sorted(out.values(), key=lambda r: -r["tokens"])


def compaction(con):
    """compaction_assembly decision mix + token relief -> {mu:{...}, cc:{zeros}}."""
    mix = dict(
        con.execute(
            """
            WITH d AS (
                SELECT unnest(json_extract(payload,'$.decisions')::JSON[]) AS dec
                FROM ev WHERE kind='compaction_assembly'
                  AND json_extract(payload,'$.decisions') IS NOT NULL
            )
            SELECT json_extract_string(dec,'$.action') AS act, count(*) AS n
            FROM d GROUP BY 1
            """
        ).fetchall()
    )
    before, after, events = con.execute(
        """
        SELECT COALESCE(sum(json_extract(payload,'$.tokens_before')::BIGINT),0),
               COALESCE(sum(json_extract(payload,'$.tokens_after')::BIGINT),0),
               count(*)
        FROM ev WHERE kind='compaction_assembly'
        """
    ).fetchone()
    mu = {a: int(mix.get(a, 0)) for a in _COMPACTION_ACTIONS}
    mu.update(before=int(before), after=int(after), events=int(events))
    cc = {a: 0 for a in _COMPACTION_ACTIONS}
    cc.update(before=0, after=0, events=0)
    return {"mu": mu, "cc": cc}


def _demo_daemon(con):
    """Pick a session with a *real* trajectory: the largest context-token range
    among sessions that have enough assemblies AND at least one compaction (so the
    sawtooth + drops are visible), not a flat fan-out parent."""
    row = con.execute(
        """
        WITH ca AS (
            SELECT daemon, kind,
                   json_extract(payload,'$.token_count_estimate')::BIGINT AS tce
            FROM ev WHERE kind IN ('context_assembly','compaction_assembly')
        )
        SELECT daemon FROM (
            SELECT daemon,
                   count(*) FILTER (WHERE kind='context_assembly' AND tce IS NOT NULL) AS n_ca,
                   max(tce) - min(tce) AS rng,
                   count(*) FILTER (WHERE kind='compaction_assembly') AS n_comp
            FROM ca GROUP BY daemon
        )
        WHERE n_ca >= 15 AND n_comp >= 1
        ORDER BY rng DESC NULLS LAST LIMIT 1
        """
    ).fetchone()
    if row:
        return row[0]
    return con.execute(
        "SELECT daemon FROM ev WHERE kind='context_assembly' "
        "GROUP BY daemon ORDER BY count(*) DESC LIMIT 1"
    ).fetchone()[0]


def context_trajectory(con, daemon=None, cap=80):
    """One session's context size over time (k tokens) + the compaction drop indices.

    Returns (traj_k, drop_indices, daemon). Downsamples to `cap` points if long.
    """
    if daemon is None:
        daemon = _demo_daemon(con)
    rows = con.execute(
        """
        SELECT json_extract(payload,'$.token_count_estimate')::BIGINT AS tce
        FROM ev
        WHERE kind='context_assembly' AND daemon = ?
          AND json_extract(payload,'$.token_count_estimate') IS NOT NULL
        ORDER BY ts
        """,
        [daemon],
    ).fetchall()
    traj = [int(r[0]) for r in rows]
    if len(traj) > cap:  # uniform stride downsample, keep endpoints
        step = len(traj) / cap
        traj = [traj[min(int(i * step), len(traj) - 1)] for i in range(cap)]
    traj_k = [round(v / 1000) for v in traj]
    drops = [i for i in range(1, len(traj_k)) if traj_k[i] < traj_k[i - 1]]
    return traj_k, drops, daemon


def stop_reason_health(con):
    """done.stop_reason distribution -> [{stop_reason, count}]."""
    rows = con.execute(
        """
        SELECT json_extract_string(payload,'$.stop_reason') AS sr, count(*) AS n
        FROM ev WHERE kind='done' AND json_extract_string(payload,'$.stop_reason') IS NOT NULL
        GROUP BY 1 ORDER BY n DESC
        """
    ).fetchall()
    return [{"stop_reason": s, "count": int(c)} for s, c in rows]


if __name__ == "__main__":
    import json

    con = engine.connect()
    print("== tool_mix ==");           print(json.dumps(tool_mix(con), indent=1))
    print("== recall ==");             print(json.dumps(recall(con), indent=1))
    print("== compaction ==");         print(json.dumps(compaction(con), indent=1))
    traj, drops, dmn = context_trajectory(con)
    print(f"== context_trajectory (daemon={dmn}, {len(traj)} pts, drops={drops}) ==")
    print(traj)
    print("== stop_reason_health =="); print(json.dumps(stop_reason_health(con), indent=1))
