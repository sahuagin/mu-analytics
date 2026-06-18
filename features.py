#!/usr/bin/env python3
"""Per-session telemetry feature table over the unified `ev` view (DuckDB only).

The shared backbone the legacy analytics built from mu_stats.sql's
harness_costs + ask_telemetry + tool_mix views (which read the sqlite sinks).
Here it comes entirely from engine.py's `ev` view — both fleets, one schema, no
sqlite (the end-state rule: when the unification is done, analytics read DuckDB,
not the old sinks). cost_usd reuses the config rate table via sample_data.

One row per canonical session (the `session` key = daemon_id:session_id for mu,
the UUID for cc), keyed by `session_ref` (fleet:session) so it joins directly to
the scans' output. Consumed by degradation_ml + anomaly_worklist (same backbone).

Run:  ./run features.py     # smoke: coverage + a few rows
"""

import datetime

import engine

# Config-driven pricing (import-safe: sample_data guards its pipeline behind
# __main__). RATES/MULT come from config.toml [rates]/[cache_multipliers].
from sample_data import MULT, RATES, _is_dashboard_noise, cost_kind, rate_key

# One query, the whole backbone. Everything is in the ev view:
#   task_telemetry -> tokens + model + provider + started_at (the cost backbone)
#   done.elapsed_ms -> per-ask latency percentiles + ask count ("calls")
#   tool_call       -> tool_calls
#   inter-event gaps -> gaps_over_5m (idle/stall signal; mu-rich, cc sparse)
_FEATURE_SQL = """
WITH tt AS (
    SELECT session, any_value(fleet) AS fleet,
           sum(COALESCE(json_extract(payload, '$.prompt_tokens')::BIGINT, 0))      AS input_tok,
           sum(COALESCE(json_extract(payload, '$.completion_tokens')::BIGINT, 0))  AS output_tok,
           sum(COALESCE(json_extract(payload, '$.cache_read_tokens')::BIGINT, 0))  AS cache_read_tok,
           sum(COALESCE(json_extract(payload, '$.cache_write_tokens')::BIGINT, 0)) AS cache_write_tok,
           count(*)                                                                AS n_tasks,
           min(COALESCE(json_extract(payload, '$.started_at_unix_ms')::BIGINT, ts)) AS started_ms,
           mode(json_extract_string(payload, '$.model'))                           AS model,
           mode(json_extract_string(payload, '$.provider_kind'))                   AS provider
    FROM ev WHERE kind = 'task_telemetry' GROUP BY session
),
done AS (
    SELECT session, count(*) AS calls,
           quantile_cont(json_extract(payload, '$.elapsed_ms')::DOUBLE, 0.50) AS wall_p50,
           quantile_cont(json_extract(payload, '$.elapsed_ms')::DOUBLE, 0.95) AS wall_p95
    FROM ev WHERE kind = 'done' AND json_extract(payload, '$.elapsed_ms') IS NOT NULL
    GROUP BY session
),
tools AS (
    SELECT session, count(*) AS tool_calls FROM ev WHERE kind = 'tool_call' GROUP BY session
),
gaps AS (
    SELECT session, count(*) FILTER (WHERE gap_ms > 300000) AS gaps_over_5m
    FROM (SELECT session, ts - lag(ts) OVER (PARTITION BY session ORDER BY id) AS gap_ms FROM ev)
    GROUP BY session
)
SELECT tt.fleet, tt.session, tt.model, tt.provider, tt.started_ms,
       tt.input_tok, tt.output_tok, tt.cache_read_tok, tt.cache_write_tok, tt.n_tasks,
       COALESCE(d.calls, 0) AS calls, COALESCE(d.wall_p50, 0) AS wall_p50,
       COALESCE(d.wall_p95, 0) AS wall_p95, COALESCE(t.tool_calls, 0) AS tool_calls,
       COALESCE(g.gaps_over_5m, 0) AS gaps_over_5m
FROM tt
LEFT JOIN done d USING (session)
LEFT JOIN tools t USING (session)
LEFT JOIN gaps g USING (session)
"""

# The numeric feature columns degradation_ml / anomaly_worklist feed to the model.
NUMERIC = [
    "calls",
    "input_tok",
    "output_tok",
    "cache_read_tok",
    "cache_write_tok",
    "cost_usd",
    "hour_of_day",
    "day_of_week",
    "wall_p50",
    "wall_p95",
    "gaps_over_5m",
    "tool_calls",
    "n_tasks",
]


def price(model, input_tok, output_tok, cache_read_tok, cache_write_tok):
    """cost_usd from the config rate table (same formula as sample_data._load);
    unlisted models price to 0.0 (flagged, never silently guessed)."""
    rr = RATES.get(rate_key(model))
    if not rr:
        return 0.0
    return round(
        (
            input_tok * rr["input"]
            + cache_write_tok * rr["input"] * MULT["write_5m"]
            + cache_read_tok * rr["input"] * MULT["read"]
            + output_tok * rr["output"]
        )
        / 1e6,
        4,
    )


def session_features(con):
    """Per-session feature dicts from the `ev` view. Each carries the NUMERIC
    columns above plus model/provider/started_at/cost_kind and a canonical
    `session_ref` (fleet:session) that joins to the scans' output. DuckDB-only."""
    cur = con.execute(_FEATURE_SQL)
    cols = [d[0] for d in cur.description]
    rows = []
    for r in cur.fetchall():
        d = dict(zip(cols, r, strict=True))
        ms = d.get("started_ms") or 0
        dt = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.UTC) if ms else None
        d["started_at"] = dt.isoformat() if dt else ""
        d["hour_of_day"] = dt.hour if dt else 0
        d["day_of_week"] = dt.weekday() if dt else 0  # 0=Mon (consistent; encoding-only)
        d["cost_usd"] = price(
            d["model"], d["input_tok"], d["output_tok"], d["cache_read_tok"], d["cache_write_tok"]
        )
        d["cost_kind"] = cost_kind(d["provider"], d["model"])
        if _is_dashboard_noise({"model": d["model"], "kind": d["cost_kind"]}):
            continue
        d["session_ref"] = f"{d['fleet']}:{d['session']}"
        rows.append(d)
    return rows


def smoke():
    if not engine.events_present():
        print("NO EVENTS")
        return
    rows = session_features(engine.connect())
    by_fleet = {}
    for r in rows:
        by_fleet.setdefault(r["fleet"], 0)
        by_fleet[r["fleet"]] += 1
    fleets = "  ".join(f"{f}={n:,}" for f, n in sorted(by_fleet.items()))
    print(f"session features: {len(rows):,} sessions   ({fleets})")
    priced = sum(1 for r in rows if r["cost_usd"] > 0)
    print(f"priced (cost_usd>0): {priced:,}   total ${sum(r['cost_usd'] for r in rows):,.2f}")
    print("sample (highest cost):")
    for r in sorted(rows, key=lambda x: -x["cost_usd"])[:5]:
        print(
            f"  {r['session_ref'][:40]:40} {r['model'][:22]:22} "
            f"calls={r['calls']:>3} tools={r['tool_calls']:>4} "
            f"in={r['input_tok']:>9,} out={r['output_tok']:>7,} ${r['cost_usd']:>8.2f}"
        )


if __name__ == "__main__":
    smoke()
