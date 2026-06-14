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
import os
import sqlite3
import tomllib
import json
import datetime
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
_cfg = tomllib.load(open(os.path.join(HERE, "config.toml"), "rb"))
RATES, MULT, PATHS = _cfg["rates"], _cfg["cache_multipliers"], _cfg["paths"]
CKP = {k.lower(): v for k, v in _cfg.get("cost_kind", {}).get("provider", {}).items()}
CKM = {k.lower(): v for k, v in _cfg.get("cost_kind", {}).get("model", {}).items()}
_KEYS = sorted(RATES.keys(), key=len, reverse=True)
_PRE = ("anthropic/", "google/", "openai/", "openrouter/", "deepseek/", "x-ai/")
HALLU = {"narrative_no_action", "hollow_commit", "lying_state"}   # mu-042 hallu set


def rate_key(m):
    if not m:
        return None
    m = m.lower()
    for p in _PRE:
        if m.startswith(p):
            m = m[len(p):]
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
    return datetime.datetime.fromtimestamp((ms or 0) / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def _load(label, db):
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT provider, model, exit_reason, outcome_class, "
            "COALESCE(tool_call_count,0) tools, COALESCE(prompt_tokens,0) inp, "
            "COALESCE(completion_tokens,0) out, COALESCE(cache_read_tokens,0) cr, "
            "COALESCE(cache_write_tokens,0) cw, started_at_unix_ms, ended_at_unix_ms FROM tasks")]
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
        r["cost"] = round((r["inp"] * rr["input"] + r["cw"] * rr["input"] * MULT["write_5m"]
                           + r["cr"] * rr["input"] * MULT["read"] + r["out"] * rr["output"]) / 1e6, 4) if rr else 0.0
        r["kind"] = cost_kind(r["provider"], r["model"])
    return rows


def build():
    rows = _load("mu", PATHS["mu_sink_db"]) + _load("cc", PATHS["cc_sink_db"])
    now = datetime.datetime.now().isoformat(timespec="seconds")
    if not rows:
        return {"as_of": now, "note": "no sink data found",
                "kpi": {"total_api_rate_equiv": 0, "by_kind": {"subscription": 0, "billed": 0, "free": 0}},
                "cost_by_kind": [{"label": k, "sessions": 0, "cost": 0} for k in ("subscription", "billed", "free")],
                "cost_by_fleet": [], "cost_by_model": [], "outcomes": [],
                "cost_composition_top_session": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                "top_sessions": [], "hallucination_by_model": [], "trend_by_day": []}

    def agg(keyfn):
        d = defaultdict(lambda: [0, 0.0])
        for r in rows:
            d[keyfn(r)][0] += 1
            d[keyfn(r)][1] += r["cost"]
        return sorted(([k, v[0], round(v[1], 2)] for k, v in d.items()), key=lambda x: -x[2])

    by_kind = [{"label": k, "sessions": s, "cost": c} for k, s, c in agg(lambda r: r["kind"])]
    by_fleet = [{"label": k, "sessions": s, "cost": c} for k, s, c in agg(lambda r: r["fleet"])]
    by_model = [{"fleet": k[0], "model": k[1], "sessions": s, "cost": c}
                for k, s, c in agg(lambda r: (r["fleet"], r["model"]))[:8]]
    outcomes = [{"outcome": k or "unclassified", "sessions": s} for k, s, c in agg(lambda r: r["outcome_class"])]

    # hallucination rate per (fleet, model): hallu-outcomes / tool-using sessions
    hd = defaultdict(lambda: [0, 0])  # (fleet, model) -> [denom, num]
    for r in rows:
        if r["tools"] > 0:
            hd[(r["fleet"], r["model"])][0] += 1
            if (r["outcome_class"] or "") in HALLU:
                hd[(r["fleet"], r["model"])][1] += 1
    hallu = sorted(
        [{"fleet": k[0], "model": k[1], "rate": round(n / d, 3), "sessions": d}
         for k, (d, n) in hd.items() if d >= 3],
        key=lambda x: -x["sessions"])[:10]

    top = sorted(rows, key=lambda r: -r["cost"])[:8]
    t = top[0]
    trr = RATES.get(rate_key(t["model"])) or {"input": 0, "output": 0}
    comp = {"input": round(t["inp"] * trr["input"] / 1e6, 2),
            "output": round(t["out"] * trr["output"] / 1e6, 2),
            "cache_read": round(t["cr"] * trr["input"] * MULT["read"] / 1e6, 2),
            "cache_write": round(t["cw"] * trr["input"] * MULT["write_5m"] / 1e6, 2)}
    top_sessions = [{"fleet": r["fleet"], "model": r["model"], "kind": r["kind"], "cost": round(r["cost"], 4),
                     "outcome": r["outcome_class"] or "unclassified", "tool_calls": r["tools"],
                     "started": _day(r["started_at_unix_ms"]), "flagged": False}
                    for r in top]

    dcost, dden, dnum = defaultdict(float), defaultdict(int), defaultdict(int)
    for r in rows:
        d = _day(r["started_at_unix_ms"] or r["ended_at_unix_ms"])
        dcost[d] += r["cost"]
        if r["tools"] > 0:
            dden[d] += 1
            if (r["outcome_class"] or "") in HALLU:
                dnum[d] += 1
    trend = [{"date": d, "cost": round(dcost[d], 2),
              "degradation": round(dnum[d] / dden[d], 3) if dden[d] else 0.0}
             for d in sorted(dcost)]

    return {
        "as_of": now,
        "note": "logs are live; every number is an as-of snapshot, not a final figure",
        "kpi": {"total_api_rate_equiv": round(sum(r["cost"] for r in rows), 2),
                "by_kind": {x["label"]: x["cost"] for x in by_kind}},
        "cost_by_kind": by_kind,
        "cost_by_fleet": by_fleet,
        "cost_by_model": by_model,
        "outcomes": outcomes,
        "cost_composition_top_session": comp,
        "top_sessions": top_sessions,
        "hallucination_by_model": hallu,
        "trend_by_day": trend,
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
