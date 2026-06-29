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


def _is_dashboard_noise(row):
    """Rows excluded from the default dashboard corpus.

    `faux` is mu's test provider/model and visually dominates recent live days with
    zero-cost, zero-tool sessions. Keep the policy intentionally narrow for now:
    local/free real model work remains visible.
    """
    return (row.get("model") or "").lower() == "faux"


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
    """Stable display+drill key, e.g. mu·a1f3c2d9.

    digest_size=4 (32-bit) not 2 (16-bit): at 16 bits the 4k+ session keys
    collided ~122 times, so two distinct sessions shared one drill/mark key.
    32 bits makes a collision over this corpus vanishingly unlikely.
    """
    h = hashlib.blake2s((task_id or "").encode(), digest_size=4).hexdigest()
    return f"{fleet}·{h}"


def _row_identity(r):
    """Search/link identity for a dashboard session row.

    Display ids stay short and stable, but review surfaces need the canonical
    terrain too: mu daemon/session refs, daemon-prefix aliases used by older
    panels, and cc task ids. These aliases are not necessarily unique; they are
    search/link affordances, not primary keys.
    """
    fleet = r.get("fleet") or "?"
    key = r.get("task_id") or ""
    display_id = _short_id(fleet, key)
    aliases = {display_id, key}
    ref = r.get("ref")
    if not ref:
        if fleet == "cc" and key:
            # Dashboard/sink task ids are "cc-<uuid>" for display + sidecar keys,
            # but the event-log canonical session_ref used by features/scans is
            # "cc:<uuid>". Export marks with the canonical ref while keeping the
            # legacy/display forms as aliases for search and old localStorage dumps.
            ref = f"cc:{key[3:]}" if key.startswith("cc-") else f"cc:{key}"
        else:
            ref = key
    if ref:
        aliases.add(ref)
    if fleet == "cc" and key:
        aliases.add(f"cc:{key}")
    daemon = r.get("daemon")
    sid = r.get("sid")
    if daemon:
        aliases.update({daemon, daemon[:4], f"mu·{daemon[:4]}"})
    if sid:
        aliases.add(sid)
    return display_id, ref, sorted(a for a in aliases if a)


def _sessionize_mu(mu_rows, sessions):
    """Fold per-task mu sink rows into real session rows via the event-log session
    map (panels.mu_sessions). The sink is task-grained; a "session" on the dashboard
    must be one (daemon, session_id), so we group the tasks of each session: cost and
    token components summed (so top-session composition still holds), tool_calls +
    model + start from the event log, outcome from the session's last task. Any sink
    task the event log didn't see (≈0 here) survives as its own row so no cost drops."""
    by_id = {r.get("task_id"): r for r in mu_rows}
    used = set()
    out = []
    for s in sessions:
        tasks = [by_id[t] for t in s["task_ids"] if t in by_id]
        if not tasks:
            continue
        used.update(t["task_id"] for t in tasks)
        last = max(tasks, key=lambda r: r.get("started_at_unix_ms") or 0)
        sess = {
            "task_id": f"{s['daemon']}/{s['sid']}",  # unique session key for _short_id
            "fleet": "mu",
            "daemon": s["daemon"],
            "sid": s["sid"],
            "ref": f"mu:{s['daemon']}:{s['sid']}",
            "model": s["model"] or last["model"],
            "provider": last.get("provider"),
            "inp": sum(r["inp"] for r in tasks),
            "out": sum(r["out"] for r in tasks),
            "cr": sum(r["cr"] for r in tasks),
            "cw": sum(r["cw"] for r in tasks),
            "cost": round(sum(r["cost"] for r in tasks), 4),
            "outcome_class": last.get("outcome_class"),
            "tools": s["tool_calls"] or sum(r["tools"] for r in tasks),
            "started_at_unix_ms": s["started_ms"] or last.get("started_at_unix_ms"),
            "ended_at_unix_ms": max((r.get("ended_at_unix_ms") or 0) for r in tasks),
            "is_child": s["is_child"],
        }
        sess["kind"] = cost_kind(sess["provider"], sess["model"])
        out.append(sess)
    for r in mu_rows:
        if r.get("task_id") not in used:
            r = dict(r)
            r["is_child"] = False
            out.append(r)
    return out


def _build_sink(mu_session_map=None, marks_by_session=None):
    marks_by_session = marks_by_session or {}
    mu_rows = _load("mu", PATHS["mu_sink_db"])
    if mu_session_map:
        mu_rows = _sessionize_mu(mu_rows, mu_session_map)
    # cc's sink is already session-grained (one row per transcript) — pass through.
    raw_rows = mu_rows + _load("cc", PATHS["cc_sink_db"])
    noise_rows = [r for r in raw_rows if _is_dashboard_noise(r)]
    rows = [r for r in raw_rows if not _is_dashboard_noise(r)]
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
            "all_sessions": [],
            "hallucination_by_model": [],
            "trend_by_day": [],
            "default_filters": {"excluded_test_sessions": 0, "excluded_test_models": []},
            "session_index": {"by_display_id": {}, "by_alias": {}},
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

    def session_row(r):
        display_id, ref, aliases = _row_identity(r)
        return {
            "id": display_id,
            "ref": ref,
            "aliases": aliases,
            "fleet": r["fleet"],
            "model": r["model"],
            "kind": r["kind"],
            "cost": round(r["cost"], 4),
            "outcome": r["outcome_class"] or "unclassified",
            "tool_calls": r["tools"],
            "started": _day(r["started_at_unix_ms"] or r["ended_at_unix_ms"]),
            "flagged": display_id in marks_by_session or ref in marks_by_session,
            "child": bool(r.get("is_child")),
        }

    top_sessions = [session_row(r) for r in top]

    # every session, newest first — the Sessions page groups these by day. The
    # drill-down transcript is NOT embedded here (the corpus is ~450 MB); it's written
    # per-session to sessions/<slug>.json by gen_dashboard and fetched on demand,
    # keyed by this same display id (_short_id).
    all_sessions = [
        session_row(r)
        for r in sorted(
            rows, key=lambda r: -(r["started_at_unix_ms"] or r["ended_at_unix_ms"] or 0)
        )
    ]
    session_index = {
        "by_display_id": {s["id"]: s["ref"] for s in all_sessions},
        "by_alias": {
            alias: s["id"]
            for s in all_sessions
            for alias in s.get("aliases", [])
            # ambiguous daemon-prefix aliases intentionally map to the newest row;
            # search still uses all aliases, this map is only a link convenience.
        },
    }

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
        "default_filters": {
            "excluded_test_sessions": len(noise_rows),
            "excluded_test_models": ["faux"] if noise_rows else [],
        },
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
        "all_sessions": all_sessions,
        "session_index": session_index,
        "hallucination_by_model": hallu,
        "trend_by_day": trend,
    }


def _event_con():
    """Open the DuckDB event-log connection, or None if duckdb/the event dir is
    missing (the dashboard then renders sink-only with explanatory banners)."""
    try:
        import engine

        if not engine.events_present():
            return None
        return engine.connect()
    except Exception as e:
        print(f"  warn: event log unavailable ({e}); rendering sink-only", file=sys.stderr)
        return None


def _event_slices(con):
    """Event-log-derived slices via DuckDB. Returns (slices, present). Degrades
    gracefully to ({}, False) if the connection is absent — the page then renders
    sink-only and its banners explain the gap."""
    if con is None:
        return {}, False
    try:
        import marks_store
        import panels

        traj, drops, _ = panels.context_trajectory(con)
        flagged_all = panels.flagged_queue(con, limit=10_000)
        flagged = flagged_all[:12]
        flagged_total = len(flagged_all)
        return {
            "marks": marks_store.read_marks(con),
            "flagged_queue": flagged,
            "flagged_queue_total": flagged_total,
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
            "delegations": panels.delegations(con),
        }, True
    except Exception as e:  # never let the event layer break the cost dashboard
        print(f"  warn: event-log slices unavailable ({e}); rendering sink-only", file=sys.stderr)
        return {}, False


# stats dir holds the refresh-produced degradation-ml.json + mu-audit-findings.tsv;
# it's the grandparent of dashboard_out (~/mu-stats/analytics/index.html -> ~/mu-stats).
_STATS_DIR = os.path.dirname(
    os.path.dirname(
        os.path.expanduser(PATHS.get("dashboard_out", "~/mu-stats/analytics/index.html"))
    )
)


def _degradation_probe(stats_dir=_STATS_DIR):
    """Fold the ML-degradation probe (degradation-ml.json) + mu-audit findings
    (mu-audit-findings.tsv) — both produced by refresh.sh — into the DATA contract's
    degradation section. The signed probe's residual tails: unnoticed = telemetry
    rosier than the operator felt (pred>obs); task_frust = operator warmer (pred<obs).
    Graceful-degrade to empty when the files are absent (CI / fresh checkout)."""
    out = {"degradation_probe": {}, "audit_findings": []}
    try:
        with open(os.path.join(stats_dir, "degradation-ml.json")) as f:
            ml = json.load(f)
        inter = [s for s in ml.get("sessions", []) if s.get("kind") == "interactive"]
        unatt = [s for s in ml.get("sessions", []) if s.get("kind") == "unattended"]
        for s in inter:
            s["resid"] = round(s.get("pred", 0) - s.get("obs", 0), 1)
        out["degradation_probe"] = {
            **ml.get("meta", {}),
            "unnoticed": sorted(inter, key=lambda s: -s["resid"])[:12],
            "task_frust": sorted(inter, key=lambda s: s["resid"])[:12],
            "unattended": sorted(unatt, key=lambda s: s.get("pred", 0))[:15],
        }
    except (OSError, ValueError) as e:
        print(f"  warn: degradation probe unavailable ({e})", file=sys.stderr)
    try:
        cols = ["ref", "first_ts", "severity", "invariant", "event_id", "detail"]
        with open(os.path.join(stats_dir, "mu-audit-findings.tsv")) as f:
            lines = f.read().splitlines()[1:]
        out["audit_findings"] = [
            dict(zip(cols, ln.split("\t"), strict=False)) for ln in lines if ln.strip()
        ]
    except OSError:
        pass
    return out


def _incident_overlay():
    """Incident reports (notes dir, via incidents.py) -> dated timeline events.
    Never fatal — a missing/unreadable notes dir just yields no incident markers."""
    try:
        import incidents

        return incidents.load()
    except Exception as e:  # noqa: BLE001 — never let the notes dir break the page
        print(f"  warn: incidents unavailable ({e})", file=sys.stderr)
        return []


def _automation_by_day(audit_findings):
    """Per-day automation-finding rollup for the timeline overlay. Today: the
    mu-audit sweep (audit_findings, dated by first_ts). The behavior-judge verdict
    sink folds in here once that runner lands — same {date,count,sev...} shape."""
    by = defaultdict(lambda: {"count": 0, "high": 0, "medium": 0, "low": 0})
    for f in audit_findings:
        day = (f.get("first_ts") or "")[:10]
        if len(day) != 10:
            continue
        sev = (f.get("severity") or "").lower()
        by[day]["count"] += 1
        if sev in ("high", "medium", "low"):
            by[day][sev] += 1
    return [{"date": d, **v} for d, v in sorted(by.items())]


def build():
    """Assemble the full dashboard contract: sink-derived cost/overview slices +
    event-log-derived rich slices + operator marks + meta flags."""
    con = _event_con()
    # The event log is the ONLY place mu's real session identity lives, so we fetch
    # the session map first and fold the sink's per-task rows into real sessions.
    mu_session_map = None
    if con is not None:
        try:
            import panels

            mu_session_map = panels.mu_sessions(con)
        except Exception as e:
            print(
                f"  warn: mu session map unavailable ({e}); sessions stay task-grained",
                file=sys.stderr,
            )
    marks_by_session = {}
    if con is not None:
        try:
            import marks_store

            marks_by_session = marks_store.read_marks_by_session(con)
        except Exception as e:
            print(f"  warn: session marks unavailable ({e}); sessions unflagged", file=sys.stderr)
    result = _build_sink(mu_session_map, marks_by_session)
    slices, present = _event_slices(con)
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
        ("flagged_queue_total", 0),
        ("context_trajectory", []),
        ("context_compactions", []),
        ("tool_mix", []),
        ("recall", []),
        ("per_ask_sessions", []),
        ("stop_reason_health", []),
        (
            "delegations",
            {
                "workers": [],
                "orchestrators": 0,
                "by_outcome": [],
                "mailbox": {"posted": 0, "consumed": 0, "by_kind": []},
            },
        ),
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
    # ML-degradation probe + mu-audit findings (refresh-produced files) -> DATA.
    result.update(_degradation_probe())
    # Overview timeline overlays: incident reports (notes dir) + per-day automation
    # findings. marks (read above into slices) are the third overlay; together they
    # ride the cost/degradation trend so the operator can eyeball correlations.
    result["incidents"] = _incident_overlay()
    result["automation_by_day"] = _automation_by_day(result.get("audit_findings", []))
    mark_summary = {
        "marks": len(slices.get("marks", [])),
        "sessions": len(marks_by_session),
        "days": len({m.get("date") for m in slices.get("marks", []) if m.get("date")}),
    }
    result["meta"] = {
        "enrichment_status": "pending_commit_enricher",
        "duckdb": present,
        "event_dir_present": present,
        "marks_n": len(slices.get("marks", [])),
        "mark_summary": mark_summary,
        "flags": {
            "overview": {"thin": False},
            "cost": {"thin": False, "cache_tier_sparse": True},
            "sessions": {
                "thin": False,
                # mu rows are grouped into real (daemon, session_id) sessions when the
                # event log is present; without it they fall back to task-grained rows.
                "grain": "session" if mu_session_map else "task",
            },
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
