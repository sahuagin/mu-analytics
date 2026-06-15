#!/usr/bin/env python3
"""Build the dashboard's DATA contract from the verified sinks.

`build()` returns exactly the object the dashboard's `const DATA` expects
(see index.html / design/template.html). `./run sample_data.py` prints it as
JSON — that printout IS the contract. gen_dashboard.py imports build() and
injects the result into the page.

Everything here is computed from the sink with the verified [rates]/[cost_kind]
config — nothing is fabricated. `flagged` is always False (operator marks are
not wired into the sink yet); `degradation` is the real daily hallucination
rate, which is artifact-laden until the commit-enricher lands (see README).
"""

import datetime
import hashlib
import json
import os
import sqlite3
import sys
import tomllib
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
# config.toml is machine-specific (gitignored); fall back to the committed example
# so imports work in CI / fresh checkouts (tests use fixtures, not these paths).
_CFG_PATH = os.path.join(HERE, "config.toml")
if not os.path.exists(_CFG_PATH):
    _CFG_PATH = os.path.join(HERE, "config.example.toml")
_cfg = tomllib.load(open(_CFG_PATH, "rb"))
RATES, MULT, PATHS = _cfg["rates"], _cfg["cache_multipliers"], _cfg["paths"]
CKP = {k.lower(): v for k, v in _cfg.get("cost_kind", {}).get("provider", {}).items()}
CKM = {k.lower(): v for k, v in _cfg.get("cost_kind", {}).get("model", {}).items()}
_KEYS = sorted(RATES.keys(), key=len, reverse=True)
_PRE = ("anthropic/", "google/", "openai/", "openrouter/", "deepseek/", "x-ai/")
HALLU = {"narrative_no_action", "hollow_commit", "lying_state"}  # mu-042 hallu set


def rate_key(m):
    if not m:
        return None
    m = m.lower()
    for p in _PRE:
        if m.startswith(p):
            m = m[len(p) :]
            break
    for k in _KEYS:
        if m == k or m.startswith(k):
            return k
    return None


def cost_kind(prov, model):
    m = (model or "").lower()
    if m == "":
        return "free"
    if m in CKM:
        return CKM[m]
    return CKP.get((prov or "").lower(), "unknown")


def _day(ms):
    return datetime.datetime.fromtimestamp((ms or 0) / 1000, tz=datetime.UTC).strftime("%Y-%m-%d")


def _load(label, db):
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in con.execute(
                "SELECT task_id, session_id, provider, model, exit_reason, outcome_class, "
                "COALESCE(tool_call_count,0) tools, COALESCE(prompt_tokens,0) inp, "
                "COALESCE(completion_tokens,0) out, COALESCE(cache_read_tokens,0) cr, "
                "COALESCE(cache_write_tokens,0) cw, started_at_unix_ms, ended_at_unix_ms FROM tasks"
            )
        ]
    except sqlite3.OperationalError:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass
    for r in rows:
        r["fleet"] = label
        rr = RATES.get(rate_key(r["model"]))
        r["cost"] = (
            round(
                (
                    r["inp"] * rr["input"]
                    + r["cw"] * rr["input"] * MULT["write_5m"]
                    + r["cr"] * rr["input"] * MULT["read"]
                    + r["out"] * rr["output"]
                )
                / 1e6,
                4,
            )
            if rr
            else 0.0
        )
        r["kind"] = cost_kind(r["provider"], r["model"])
    return rows


def _short_id(fleet, task_id):
    """Stable display+drill key, e.g. mu·a1f3."""
    h = hashlib.blake2s((task_id or "").encode(), digest_size=2).hexdigest()
    return f"{fleet}·{h}"


def _build_sink():
    rows = _load("mu", PATHS["mu_sink_db"]) + _load("cc", PATHS["cc_sink_db"])
    now = datetime.datetime.now().isoformat(timespec="seconds")
    if not rows:
        return {
            "as_of": now,
            "note": "no sink data found",
            "kpi": {
                "total_api_rate_equiv": 0,
                "by_kind": {"subscription": 0, "billed": 0, "free": 0},
            },
            "cost_by_kind": [
                {"label": k, "sessions": 0, "cost": 0} for k in ("subscription", "billed", "free")
            ],
            "cost_by_fleet": [],
            "cost_by_model": [],
            "outcomes": [],
            "cost_composition_top_session": {
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
            },
            "top_sessions": [],
            "hallucination_by_model": [],
            "trend_by_day": [],
        }

    def agg(keyfn):
        d = defaultdict(lambda: [0, 0.0])
        for r in rows:
            d[keyfn(r)][0] += 1
            d[keyfn(r)][1] += r["cost"]
        return sorted(([k, v[0], round(v[1], 2)] for k, v in d.items()), key=lambda x: -x[2])

    by_kind = [{"label": k, "sessions": s, "cost": c} for k, s, c in agg(lambda r: r["kind"])]
    by_fleet = [{"label": k, "sessions": s, "cost": c} for k, s, c in agg(lambda r: r["fleet"])]
    by_model = [
        {"fleet": k[0], "model": k[1], "sessions": s, "cost": c}
        for k, s, c in agg(lambda r: (r["fleet"], r["model"]))[:8]
    ]
    outcomes = [
        {"outcome": k or "unclassified", "sessions": s}
        for k, s, _c in agg(lambda r: r["outcome_class"])
    ]

    # hallucination rate per (fleet, model): hallu-outcomes / tool-using sessions
    hd = defaultdict(lambda: [0, 0])  # (fleet, model) -> [denom, num]
    for r in rows:
        if r["tools"] > 0:
            hd[(r["fleet"], r["model"])][0] += 1
            if (r["outcome_class"] or "") in HALLU:
                hd[(r["fleet"], r["model"])][1] += 1
    hallu = sorted(
        [
            {"fleet": k[0], "model": k[1], "rate": round(n / d, 3), "sessions": d}
            for k, (d, n) in hd.items()
            if d >= 3
        ],
        key=lambda x: -x["sessions"],
    )[:10]

    top = sorted(rows, key=lambda r: -r["cost"])[:8]
    t = top[0]
    trr = RATES.get(rate_key(t["model"])) or {"input": 0, "output": 0}
    comp = {
        "input": round(t["inp"] * trr["input"] / 1e6, 2),
        "output": round(t["out"] * trr["output"] / 1e6, 2),
        "cache_read": round(t["cr"] * trr["input"] * MULT["read"] / 1e6, 2),
        "cache_write": round(t["cw"] * trr["input"] * MULT["write_5m"] / 1e6, 2),
    }
    top_sessions = [
        {
            "id": _short_id(r["fleet"], r.get("task_id")),
            "fleet": r["fleet"],
            "model": r["model"],
            "kind": r["kind"],
            "cost": round(r["cost"], 4),
            "outcome": r["outcome_class"] or "unclassified",
            "tool_calls": r["tools"],
            "started": _day(r["started_at_unix_ms"]),
            "flagged": False,
        }
        for r in top
    ]

    dcost, dden, dnum = defaultdict(float), defaultdict(int), defaultdict(int)
    for r in rows:
        d = _day(r["started_at_unix_ms"] or r["ended_at_unix_ms"])
        dcost[d] += r["cost"]
        if r["tools"] > 0:
            dden[d] += 1
            if (r["outcome_class"] or "") in HALLU:
                dnum[d] += 1
    trend = [
        {
            "date": d,
            "cost": round(dcost[d], 2),
            "degradation": round(dnum[d] / dden[d], 3) if dden[d] else 0.0,
        }
        for d in sorted(dcost)
    ]

    return {
        "as_of": now,
        "note": "logs are live; every number is an as-of snapshot, not a final figure",
        "kpi": {
            "total_api_rate_equiv": round(sum(r["cost"] for r in rows), 2),
            "by_kind": {x["label"]: x["cost"] for x in by_kind},
        },
        "cost_by_kind": by_kind,
        "cost_by_fleet": by_fleet,
        "cost_by_model": by_model,
        "outcomes": outcomes,
        "cost_composition_top_session": comp,
        "top_sessions": top_sessions,
        "hallucination_by_model": hallu,
        "trend_by_day": trend,
    }


def _event_slices():
    """Event-log-derived slices via DuckDB. Returns (slices, present). Degrades
    gracefully to ({}, False) if duckdb/the event dir is missing — the page then
    renders sink-only and its banners explain the gap."""
    try:
        import engine
        import marks_store
        import panels

        if not engine.events_present():
            return {}, False
        con = engine.connect()
        traj, drops, _ = panels.context_trajectory(con)
        return {
            "marks": marks_store.read_marks(con),
            "flagged_queue": panels.flagged_queue(con),
            "compaction": panels.compaction(con),
            "context_trajectory": traj,
            "context_compactions": drops,
            "tool_mix": panels.tool_mix(con),
            "recall": panels.recall(con),
            "cache_econ": panels.cache_econ(con),
            "per_ask_sessions": panels.per_ask_sessions(con),
            "stop_reason_health": panels.stop_reason_health(con),
            "degradation_by_day": panels.degradation_by_day(con),
            "degradation_rate": panels.degradation_rate(con),
        }, True
    except Exception as e:  # never let the event layer break the cost dashboard
        print(f"  warn: event-log slices unavailable ({e}); rendering sink-only", file=sys.stderr)
        return {}, False


def build():
    """Assemble the full dashboard contract: sink-derived cost/overview slices +
    event-log-derived rich slices + operator marks + meta flags."""
    result = _build_sink()
    slices, present = _event_slices()
    # the event log carries the REAL degradation signal; overlay it onto the trend
    # (replacing the sink's narrative_no_action artifact) and surface the headline rate
    deg_day = slices.pop("degradation_by_day", {})
    deg_rate = slices.pop("degradation_rate", None)
    result.update(slices)
    if deg_day:
        for d in result.get("trend_by_day", []):
            d["degradation"] = deg_day.get(d["date"], 0.0)
    result["degradation_rate"] = deg_rate if deg_rate is not None else 0.0
    # defaults so the page renders (with banners) even when the event layer is absent
    _zero_comp = {
        "kept": 0,
        "dropped": 0,
        "summarized": 0,
        "failed": 0,
        "before": 0,
        "after": 0,
        "events": 0,
    }
    for k, default in (
        ("marks", []),
        ("flagged_queue", []),
        ("context_trajectory", []),
        ("context_compactions", []),
        ("tool_mix", []),
        ("recall", []),
        ("per_ask_sessions", []),
        ("stop_reason_health", []),
        ("compaction", {"mu": dict(_zero_comp), "cc": dict(_zero_comp)}),
        (
            "cache_econ",
            {
                "median_gap_min": 0,
                "p90_gap_min": 0,
                "save_pct": 0,
                "save_pct_p90": 0,
                "w5_tokens": 0,
                "w1_tokens": 0,
                "read_tokens": 0,
            },
        ),
    ):
        result.setdefault(k, default)
    result["meta"] = {
        "enrichment_status": "pending_commit_enricher",
        "duckdb": present,
        "event_dir_present": present,
        "marks_n": len(slices.get("marks", [])),
        "flags": {
            "overview": {"thin": False},
            "cost": {"thin": False, "cache_tier_sparse": True},
            "sessions": {"thin": False},
            "behavioral": {
                "thin": True,
                "cc_behavioral_empty": True,
                "reason": "cc bridge emits no stop_reason/tool_result yet",
            },
            "internalops": {"fleetScope": "mu", "thin": True, "compaction_actions_partial": True},
        },
    }
    return result


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
