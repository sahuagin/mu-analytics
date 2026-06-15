#!/usr/bin/env python3
"""Per-page analytics slices derived from the event-log JSONL via DuckDB.

Each builder takes a connection with the `ev` view registered (see engine.py)
and returns JSON-able structures matching the shapes proto/index.html consumes.
Cost math reuses sample_data's rate table + formula — never re-derived here.

`./run panels.py` prints each slice (a verification harness).
"""
import math

import engine
from sample_data import RATES, MULT, rate_key

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


# A clean finish is end_turn / tool_use; everything else is a degraded finish.
_DEGRADED_STOP = ("degraded_eof", "aborted", "error", "max_tokens", "iteration_cap")


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


def degradation_by_day(con):
    """Real per-day degradation = share of done events with a non-clean stop_reason
    (degraded_eof/aborted/error/max_tokens/iteration_cap). This is the event-log
    signal, NOT the sink's narrative_no_action artifact. Returns {date: rate}."""
    bad = "','".join(_DEGRADED_STOP)
    rows = con.execute(
        f"""
        SELECT strftime(to_timestamp(ts/1000), '%Y-%m-%d') AS day,
               count(*) FILTER (WHERE sr IN ('{bad}')) AS bad,
               count(*) AS tot
        FROM (SELECT ts, json_extract_string(payload,'$.stop_reason') AS sr
              FROM ev WHERE kind='done' AND json_extract_string(payload,'$.stop_reason') IS NOT NULL)
        GROUP BY 1
        """
    ).fetchall()
    return {day: round(b / t, 3) if t else 0.0 for day, b, t in rows}


def degradation_rate(con):
    """Overall non-clean-finish rate across all done events (the real headline)."""
    bad = "','".join(_DEGRADED_STOP)
    row = con.execute(
        f"""
        SELECT count(*) FILTER (WHERE sr IN ('{bad}')) AS bad, count(*) AS tot
        FROM (SELECT json_extract_string(payload,'$.stop_reason') AS sr
              FROM ev WHERE kind='done' AND json_extract_string(payload,'$.stop_reason') IS NOT NULL)
        """
    ).fetchone()
    return round(row[0] / row[1] * 100, 1) if row[1] else 0.0


def _daemon_model(con, daemon):
    row = con.execute(
        "SELECT arg_max(json_extract_string(payload,'$.model'), ts) "
        "FROM ev WHERE kind='task_telemetry' AND daemon = ?",
        [daemon],
    ).fetchone()
    return row[0] if row else None


# --- flagged queue: degradation signals straight from the event log ---
_REASON_CONF = {"deg": "Probable 0.62", "err": "Probable 0.70",
                "callout": "Definite 0.88", "tomb": "Inferred 0.41"}
_SEVERITY = {"deg": 0, "err": 1, "callout": 2, "tomb": 3}


def flagged_queue(con, limit=12):
    """Sessions worth a human look — degraded stop_reasons, error exits, tool-error
    loops, self-flag callouts, autonomy terminations. mu-only in v1."""
    rows = con.execute(
        """
        WITH tel AS (
            SELECT daemon,
                   arg_max(json_extract_string(payload,'$.model'), ts) AS model
            FROM ev WHERE kind='task_telemetry' GROUP BY daemon
        ),
        flags AS (
            SELECT daemon, 'deg' AS reason,
                   'stop_reason='||json_extract_string(payload,'$.stop_reason')||' mid-task' AS why
            FROM ev WHERE kind='done'
              AND json_extract_string(payload,'$.stop_reason') IN
                  ('degraded_eof','max_tokens','iteration_cap','aborted')
            UNION ALL
            SELECT daemon, 'err',
                   'exit_reason='||json_extract_string(payload,'$.exit_reason')
            FROM ev WHERE kind='task_telemetry'
              AND json_extract_string(payload,'$.exit_reason') IN ('error','cancelled')
            UNION ALL
            SELECT daemon, 'err', (cnt::VARCHAR)||' tool_result.is_error in session'
            FROM (SELECT daemon, count(*) AS cnt FROM ev
                  WHERE kind='tool_result' AND json_extract(payload,'$.is_error')::BOOLEAN
                  GROUP BY daemon HAVING count(*) >= 5)
            UNION ALL
            SELECT daemon, 'callout',
                   'self-flag: '||COALESCE(json_extract_string(payload,'$.title'),'(callout)')
            FROM ev WHERE kind='callout'
            UNION ALL
            SELECT daemon, 'deg',
                   'autonomous_terminated: '||COALESCE(json_extract_string(payload,'$.reason'),'?')
            FROM ev WHERE kind='autonomous_terminated'
        )
        SELECT f.daemon, f.reason, any_value(f.why) AS why, any_value(t.model) AS model
        FROM flags f LEFT JOIN tel t USING (daemon)
        GROUP BY f.daemon, f.reason
        """
    ).fetchall()
    # round-robin across reason types so the queue shows variety, severest first
    from collections import defaultdict
    buckets = defaultdict(list)
    for r in rows:
        buckets[r[1]].append(r)
    order = sorted(buckets, key=lambda k: _SEVERITY.get(k, 9))
    interleaved = []
    while len(interleaved) < limit and any(buckets[k] for k in order):
        for k in order:
            if buckets[k]:
                interleaved.append(buckets[k].pop(0))
                if len(interleaved) >= limit:
                    break
    out = []
    for daemon, reason, why, model in interleaved:
        out.append({
            "id": "mu·" + daemon[:4],
            "fleet": "mu",
            "model": model or "—",
            "reason": reason,
            "why": why,
            "conf": _REASON_CONF.get(reason, "Inferred 0.40"),
        })
    return out


# --- cache economics: real median inter-ask gap + savings at that gap ---
def _blended_session_cost(gap_min, ttl_min, write_mult, rate=5.0, prefix=60000, asks=40):
    """Same model the dashboard chart uses: a stable cached prefix re-read each ask
    (0.10x) unless the cache lapsed (gap>ttl), which forces a write at write_mult."""
    base_read = prefix * rate / 1e6 * MULT["read"]
    expired = math.exp(-ttl_min / gap_min) if gap_min > 0 else 0.0
    per_ask = (1 - expired) * base_read + expired * (prefix * rate / 1e6 * write_mult)
    return asks * per_ask


def cache_econ(con):
    """Real inter-ask gap (between consecutive model calls) + 1h-vs-5m savings AT
    that gap, plus how much 5m/1h cache the corpus actually writes."""
    gp = con.execute(
        """
        WITH gaps AS (
            SELECT (ts - lag(ts) OVER (PARTITION BY daemon ORDER BY ts)) / 60000.0 AS gm
            FROM ev WHERE kind='assistant_message_event'
        )
        SELECT median(gm) FILTER (WHERE gm BETWEEN 0 AND 120),
               quantile_cont(gm, 0.9) FILTER (WHERE gm BETWEEN 0 AND 120)
        FROM gaps
        """
    ).fetchone()
    median_gap = round(gp[0] or 0.0, 2)
    p90_gap = round(gp[1] or 0.0, 2)
    vol = con.execute(
        """
        SELECT COALESCE(sum(json_extract(payload,'$.cache_write_5m_tokens')::BIGINT),0),
               COALESCE(sum(json_extract(payload,'$.cache_write_1h_tokens')::BIGINT),0),
               COALESCE(sum(json_extract(payload,'$.cache_read_tokens')::BIGINT),0)
        FROM ev WHERE kind='task_telemetry'
        """
    ).fetchone()

    def save_at(g):
        if g <= 0:
            return 0.0
        c5 = _blended_session_cost(g, 5, MULT["write_5m"])
        c1 = _blended_session_cost(g, 60, MULT["write_1h"])
        return round((c5 - c1) / c5 * 100, 1) if c5 else 0.0

    return {
        "median_gap_min": median_gap,
        "p90_gap_min": p90_gap,
        "save_pct": save_at(median_gap),       # honest: small when turns are fast
        "save_pct_p90": save_at(p90_gap),       # where 1h actually starts to pay
        "w5_tokens": int(vol[0]), "w1_tokens": int(vol[1]), "read_tokens": int(vol[2]),
    }


# --- per-ask cost for one session (real turns) ---
def per_ask(con, daemon=None, limit=28):
    """Per-turn cost within a session; amber = a turn that paid a cache write."""
    if daemon is None:
        daemon = con.execute(
            "SELECT daemon FROM ev WHERE kind='assistant_message_event' "
            "GROUP BY daemon HAVING count(*) BETWEEN 12 AND 60 ORDER BY count(*) DESC LIMIT 1"
        ).fetchone()[0]
    model = _daemon_model(con, daemon)
    rr = RATES.get(rate_key(model)) or RATES["claude-opus-4-8"]
    rows = con.execute(
        """
        SELECT json_extract(payload,'$.message.usage.input_tokens')::BIGINT AS inp,
               json_extract(payload,'$.message.usage.output_tokens')::BIGINT AS out,
               COALESCE(json_extract(payload,'$.message.usage.cache_read_input_tokens')::BIGINT,0) AS cr,
               COALESCE(json_extract(payload,'$.message.usage.cache_creation_input_tokens')::BIGINT,0) AS cw
        FROM ev WHERE kind='assistant_message_event' AND daemon = ?
        ORDER BY ts LIMIT ?
        """,
        [daemon, limit],
    ).fetchall()
    out = []
    for i, (inp, o, cr, cw) in enumerate(rows, 1):
        cost = ((inp or 0) * rr["input"]
                + (cr or 0) * rr["input"] * MULT["read"]
                + (cw or 0) * rr["input"] * MULT["write_5m"]
                + (o or 0) * rr["output"]) / 1e6
        out.append({"i": i, "cost": round(cost, 4), "rewrite_5m": bool(cw)})
    return {"daemon": daemon, "model": model, "asks": out}


if __name__ == "__main__":
    import json

    con = engine.connect()

    def show(name, val):
        print(f"== {name} ==")
        print(json.dumps(val, indent=1))

    show("tool_mix", tool_mix(con))
    show("recall", recall(con))
    show("compaction", compaction(con))
    traj, drops, dmn = context_trajectory(con)
    print(f"== context_trajectory (daemon={dmn}, {len(traj)} pts, drops={drops}) ==")
    print(traj)
    show("stop_reason_health", stop_reason_health(con))
    show("flagged_queue", flagged_queue(con))
    show("cache_econ", cache_econ(con))
    pa = per_ask(con)
    print(f"== per_ask (daemon={pa['daemon']}, model={pa['model']}, {len(pa['asks'])} asks) ==")
    print(json.dumps(pa["asks"][:8], indent=1))
