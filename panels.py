#!/usr/bin/env python3
"""Per-page analytics slices derived from the event-log JSONL via DuckDB.

Each builder takes a connection with the `ev` view registered (see engine.py)
and returns JSON-able structures matching the shapes proto/index.html consumes.
Cost math reuses sample_data's rate table + formula — never re-derived here.

`./run panels.py` prints each slice (a verification harness).
"""

import datetime
import json
import math
import os

import engine
from sample_data import MULT, RATES, _short_id, rate_key

# Source labels for recall provenance -> the proto's display strings.
_SRC_LABEL = {
    "project_file": "ProjectFile",
    "filesystem": "ProjectFile",
    "memory": "Memory",
    "bootloader": "Bootloader",
}
_COMPACTION_ACTIONS = ("kept", "dropped", "summarized", "failed")


def _normalize_tool_name(name):
    """Normalize superficial tool spelling so the mix is comparable by fleet."""
    t = (name or "unknown").strip()
    low = t.lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "read": "read",
        "file_read": "read",
        "bash": "bash",
        "shell": "bash",
        "edit": "edit",
        "str_replace_editor": "edit",
        "write": "write",
        "grep": "grep",
        "rg": "grep",
        "glob": "glob",
        "webfetch": "web_fetch",
        "web_fetch": "web_fetch",
    }
    return aliases.get(low, low)


def tool_mix(con, limit=12):
    """tool_call distribution normalized by tool name and split by fleet."""
    rows = con.execute(
        """
        SELECT fleet, json_extract_string(payload,'$.name') AS tool, count(*) AS count
        FROM ev WHERE kind='tool_call' AND json_extract_string(payload,'$.name') IS NOT NULL
        GROUP BY 1,2 ORDER BY count DESC
        """
    ).fetchall()
    by_tool = {}
    for fleet, tool, count in rows:
        key = _normalize_tool_name(tool)
        rec = by_tool.setdefault(key, {"tool": key, "count": 0, "mu": 0, "cc": 0})
        rec["count"] += int(count)
        if fleet in ("mu", "cc"):
            rec[fleet] += int(count)
    return sorted(by_tool.values(), key=lambda r: (-r["count"], r["tool"]))[:limit]


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


def mu_sessions(con):
    """The real mu session unit — one row per (daemon, session_id), NOT per task.

    The sink carries one row per *task* and its `session_id` column is a useless
    per-daemon counter ("session-1/2/3"), so grouping the sink alone can't recover
    sessions. Session identity lives only in the event log: the dir layout
    `events/<daemon>/<session_id>.jsonl`. Each `task_telemetry` event sits in its
    session's file AND carries the sink `task_id`, so it bridges the two stores.

    Returns one dict per session that ran >=1 task::

        {daemon, sid, started_ms, model, task_ids:[...], tool_calls, is_child}

    `is_child` marks sub-agent / branched sessions (a `session_created` with a
    parent_session_id or branched_at_parent_event_id) — kept flat, just tagged.
    sample_data joins sink cost onto `task_ids` to build the session's cost/outcome.
    """
    rows = con.execute(
        """
        WITH tt AS (
            SELECT daemon, session_id AS sid,
                   json_extract_string(payload,'$.task_id') AS tid,
                   json_extract_string(payload,'$.model')   AS model,
                   ts
            FROM ev WHERE kind='task_telemetry'
              AND json_extract_string(payload,'$.task_id') IS NOT NULL
        ),
        tools AS (
            SELECT daemon, session_id AS sid, count(*) AS n
            FROM ev WHERE kind='tool_call' GROUP BY 1,2
        ),
        kids AS (
            SELECT daemon, session_id AS sid,
                   max(CASE WHEN json_extract_string(payload,'$.parent_session_id') IS NOT NULL
                            OR json_extract_string(payload,'$.branched_at_parent_event_id') IS NOT NULL
                       THEN 1 ELSE 0 END) AS is_child
            FROM ev WHERE kind='session_created' GROUP BY 1,2
        )
        SELECT t.daemon, t.sid,
               min(t.ts)                       AS started_ms,
               arg_max(t.model, t.ts)          AS model,
               list(DISTINCT t.tid)            AS task_ids,
               COALESCE(any_value(tl.n), 0)    AS tool_calls,
               COALESCE(any_value(k.is_child), 0) AS is_child
        FROM tt t
        LEFT JOIN tools tl ON tl.daemon=t.daemon AND tl.sid=t.sid
        LEFT JOIN kids  k  ON k.daemon=t.daemon  AND k.sid=t.sid
        GROUP BY t.daemon, t.sid
        """
    ).fetchall()
    out = []
    for daemon, sid, started_ms, model, task_ids, tool_calls, is_child in rows:
        out.append(
            {
                "daemon": daemon,
                "sid": sid,
                "started_ms": int(started_ms or 0),
                "model": model,
                "task_ids": [t for t in task_ids if t],
                "tool_calls": int(tool_calls),
                "is_child": bool(is_child),
            }
        )
    return out


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
    row = con.execute(
        "SELECT daemon FROM ev WHERE kind='context_assembly' "
        "GROUP BY daemon ORDER BY count(*) DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


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
_REASON_CONF = {
    "deg": "Probable 0.62",
    "err": "Probable 0.70",
    "callout": "Definite 0.88",
    "tomb": "Inferred 0.41",
}
_SEVERITY = {"deg": 0, "err": 1, "callout": 2, "tomb": 3}


def flagged_queue(con, limit=12):
    """Sessions worth a human look — degraded stop_reasons, error exits, tool-error
    loops, self-flag callouts, autonomy terminations. mu-only in v1."""
    rows = con.execute(
        """
        WITH tel AS (
            SELECT daemon, session_id AS sid,
                   arg_max(json_extract_string(payload,'$.model'), ts) AS model
            FROM ev WHERE kind='task_telemetry' GROUP BY daemon, session_id
        ),
        flags AS (
            SELECT daemon, session_id AS sid, 'deg' AS reason,
                   'stop_reason='||json_extract_string(payload,'$.stop_reason')||' mid-task' AS why
            FROM ev WHERE kind='done'
              AND json_extract_string(payload,'$.stop_reason') IN
                  ('degraded_eof','max_tokens','iteration_cap','aborted')
            UNION ALL
            SELECT daemon, session_id AS sid, 'err',
                   'exit_reason='||json_extract_string(payload,'$.exit_reason')
            FROM ev WHERE kind='task_telemetry'
              AND json_extract_string(payload,'$.exit_reason') IN ('error','cancelled')
            UNION ALL
            SELECT daemon, session_id AS sid, 'err', (cnt::VARCHAR)||' tool_result.is_error in session'
            FROM (SELECT daemon, session_id, count(*) AS cnt FROM ev
                  WHERE kind='tool_result' AND json_extract(payload,'$.is_error')::BOOLEAN
                  GROUP BY daemon, session_id HAVING count(*) >= 5)
            UNION ALL
            SELECT daemon, session_id AS sid, 'callout',
                   'self-flag: '||COALESCE(json_extract_string(payload,'$.title'),'(callout)')
            FROM ev WHERE kind='callout'
            UNION ALL
            SELECT daemon, session_id AS sid, 'deg',
                   'autonomous_terminated: '||COALESCE(json_extract_string(payload,'$.reason'),'?')
            FROM ev WHERE kind='autonomous_terminated'
        )
        SELECT f.daemon, f.sid, f.reason, any_value(f.why) AS why, any_value(t.model) AS model
        FROM flags f LEFT JOIN tel t ON t.daemon=f.daemon AND t.sid=f.sid
        GROUP BY f.daemon, f.sid, f.reason
        """
    ).fetchall()
    # round-robin across reason types so the queue shows variety, severest first
    from collections import defaultdict

    buckets = defaultdict(list)
    for r in rows:
        buckets[r[2]].append(r)
    order = sorted(buckets, key=lambda k: _SEVERITY.get(k, 9))
    interleaved = []
    while len(interleaved) < limit and any(buckets[k] for k in order):
        for k in order:
            if buckets[k]:
                interleaved.append(buckets[k].pop(0))
                if len(interleaved) >= limit:
                    break
    out = []
    for daemon, sid, reason, why, model in interleaved:
        key = f"{daemon}/{sid}" if sid else daemon
        out.append(
            {
                "id": "mu·" + daemon[:4],
                "session_id": _short_id("mu", key),
                "session_ref": f"mu:{daemon}:{sid}" if sid else f"mu:{daemon}",
                "fleet": "mu",
                "model": model or "—",
                "reason": reason,
                "why": why,
                "conf": _REASON_CONF.get(reason, "Inferred 0.40"),
            }
        )
    return out


def frustration_signals(con, limit=400):
    """Per-session OPERATOR-frustration signal, CROSS-FLEET, from scans.scan_frustration
    over the `ev` view. Surfaces sessions where the operator's own language shows
    frustration (markers like 'stop', "I didn't ask", 'you keep', 'again?') and
    abrupt+frustrated endings. This is the signal that was already being computed and
    then discarded into the null ML probe — here it becomes a first-class attention row.

    Returns rows shaped for the behavioral attention queue:
        [{session_ref, fleet, reason, severity, hits, markers, ending, why}]
    sorted worst-first (high severity, then most markers)."""
    import scans

    hit_rows, _all_rows, _totals = scans.scan_frustration(con)
    rows = []
    for ref, _win, hits, _n_user, markers, _started, ending in hit_rows:
        fleet = ref.split(":", 1)[0]
        abrupt = "frustrated" in str(ending)
        severity = "high" if (hits >= 5 or abrupt) else "med" if hits >= 2 else "low"
        marker_list = list(markers or [])
        why = f"{hits} operator-frustration marker{'' if hits == 1 else 's'}"
        if marker_list:
            why += f" ({', '.join(marker_list[:3])})"
        if abrupt:
            why += "; abrupt+frustrated exit"
        rows.append(
            {
                "session_ref": ref,
                "fleet": fleet,
                "reason": "frustration",
                "severity": severity,
                "hits": int(hits),
                "markers": marker_list[:4],
                "ending": ending,
                "why": why,
            }
        )
    sev_rank = {"high": 0, "med": 1, "low": 2}
    rows.sort(key=lambda r: (sev_rank.get(r["severity"], 9), -r["hits"]))
    return rows[:limit]


def judge_verdicts(limit=400):
    """Per-session BEHAVIOR-JUDGE signal, from the semantic judge's own store
    (judge_store / data/judge.sqlite) — NOT the `ev` view. Surfaces sessions where the
    separate-LLM judge ruled that one of the 5 failure classes occurred (false_success /
    map_as_terrain / scope_overreach / relitigation / dismissiveness). This is the
    'built-but-stranded' detector the whole thread was after: the judge writes verdicts
    to its store; here they become first-class attention rows that fuse with the
    runtime/frustration/audit signals on the same canonical session_ref.

    Only FIRING verdicts (occurred=1) enter the queue; a session with several fired
    classes collapses to ONE row (worst severity, most classes first), so corroboration
    counts it as one judge source rather than five.

    Returns rows shaped for the behavioral attention queue:
        [{session_ref, fleet, reason, severity, behaviors, n, why}]."""
    import judge_store

    sev_rank = {"high": 0, "med": 1, "medium": 1, "low": 2}
    by_session = {}
    for v in judge_store.read_verdicts(only_occurred=True):
        by_session.setdefault(v["session_ref"], []).append(v)
    rows = []
    for ref, verdicts in by_session.items():
        fleet = (ref or "").split(":", 1)[0]
        behaviors = sorted(v["behavior"] for v in verdicts)
        worst = min(
            (str(v.get("severity") or "low").lower() for v in verdicts),
            key=lambda s: sev_rank.get(s, 9),
        )
        severity = (
            "med" if worst == "medium" else worst if worst in ("high", "med", "low") else "med"
        )
        plural = "" if len(behaviors) == 1 else "es"
        rows.append(
            {
                "session_ref": ref,
                "fleet": fleet,
                "reason": "judge",
                "severity": severity,
                "behaviors": behaviors,
                "n": len(behaviors),
                "why": f"judge flagged {len(behaviors)} failure class{plural}: {', '.join(behaviors)}",
            }
        )
    rows.sort(key=lambda r: (sev_rank.get(r["severity"], 9), -r["n"]))
    return rows[:limit]


_DELEGATION_KINDS = (
    "worker_spawned",
    "worker_exited",
    "worker_failed",
    "worker_timeout",
    "mailbox_message_posted",
    "mailbox_message_consumed",
)


def delegations(con, now_ms=None, stale_after_ms=6 * 60 * 60 * 1000):
    """Worker-orchestration slice for the Delegations page (was a stub): each spawned
    worker (pot/model/prompt) paired best-effort — by order within the orchestrator
    session, since the events carry no shared worker id — with its terminal event
    (exit_code / failed reason / timeout), plus the session's mailbox traffic.
    mu-native (cc emits no worker/mailbox events). Returns a Sessions-style filterable
    worker list + outcome/mailbox aggregates.

    A spawn with no matching terminal event is only "running" while recent. Old
    unmatched spawns are `unknown-stale`: the event log can no longer distinguish a
    still-live worker from a missing terminal event, and showing May-old tests as
    actively running makes the metric aim at the wrong target.
    """
    kinds = "','".join(_DELEGATION_KINDS)
    rows = con.execute(
        f"SELECT fleet, session, kind, ts, payload FROM ev "
        f"WHERE kind IN ('{kinds}') ORDER BY fleet, session, id"
    ).fetchall()

    sess = {}
    for fleet, session, kind, ts, payload in rows:
        p = json.loads(payload) if isinstance(payload, str) else payload
        e = sess.setdefault(
            (fleet, session), {"spawn": [], "term": [], "posted": 0, "consumed": 0, "mk": {}}
        )
        if kind == "worker_spawned":
            e["spawn"].append((ts, p))
        elif kind in ("worker_exited", "worker_failed", "worker_timeout"):
            e["term"].append((kind, p))
        elif kind == "mailbox_message_posted":
            e["posted"] += 1
            mk = p.get("message_kind") or "?"
            e["mk"][mk] = e["mk"].get(mk, 0) + 1
        else:  # mailbox_message_consumed
            e["consumed"] += 1

    workers, by_outcome, mk_total = [], {}, {}
    posted = consumed = 0
    for (fleet, session), e in sess.items():
        ref = f"{fleet}:{session}"
        display_id = _short_id("mu", session) if fleet == "mu" else _short_id(fleet, session)
        posted += e["posted"]
        consumed += e["consumed"]
        for k, n in e["mk"].items():
            mk_total[k] = mk_total.get(k, 0) + n
        for i, (ts, sp) in enumerate(e["spawn"]):
            outcome, detail, elapsed = "running", "", None
            if i < len(e["term"]):
                tk, tp = e["term"][i]
                if tk == "worker_exited":
                    code = tp.get("exit_code")
                    outcome = "exited" if code == 0 else "exit-nonzero"
                    detail, elapsed = f"exit {code}", tp.get("elapsed_ms")
                elif tk == "worker_failed":
                    outcome, detail = "failed", tp.get("reason") or ""
                else:  # worker_timeout
                    outcome, detail, elapsed = "timeout", "timed out", tp.get("elapsed_ms")
            elif now_ms is None:
                now_ms = int(datetime.datetime.now(tz=datetime.UTC).timestamp() * 1000)
            if outcome == "running" and now_ms is not None and ts and now_ms - ts > stale_after_ms:
                outcome = "unknown-stale"
                detail = "no terminal event recorded"
            by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
            workers.append(
                {
                    "session_ref": ref,
                    "session_id": display_id,
                    "pot": sp.get("pot_name", ""),
                    "model": sp.get("model") or "unknown",
                    "prompt": (sp.get("prompt_summary") or "")[:140],
                    "started": datetime.datetime.fromtimestamp(
                        (ts or 0) / 1000, tz=datetime.UTC
                    ).isoformat(),
                    "outcome": outcome,
                    "detail": detail,
                    "elapsed_ms": elapsed,
                    "mailbox": e["posted"] + e["consumed"],
                }
            )
    workers.sort(key=lambda w: w["started"], reverse=True)
    return {
        "workers": workers,
        "orchestrators": len(sess),
        "by_outcome": [
            {"outcome": k, "n": v} for k, v in sorted(by_outcome.items(), key=lambda x: -x[1])
        ],
        "mailbox": {
            "posted": posted,
            "consumed": consumed,
            "by_kind": [
                {"kind": k, "n": v} for k, v in sorted(mk_total.items(), key=lambda x: -x[1])
            ],
        },
    }


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
            SELECT (ts - lag(ts) OVER (PARTITION BY fleet, daemon, session_id ORDER BY ts))
                     / 60000.0 AS gm
            FROM ev WHERE kind='assistant_message_event'
        )
        SELECT median(gm) FILTER (WHERE gm BETWEEN 0 AND 120),
               quantile_cont(gm, 0.9) FILTER (WHERE gm BETWEEN 0 AND 120),
               count(*) FILTER (WHERE gm BETWEEN 0 AND 120),
               count(*) FILTER (WHERE gm > 5 AND gm <= 60),
               count(*) FILTER (WHERE gm BETWEEN 4 AND 6),
               count(*) FILTER (WHERE gm > 60)
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
        "save_pct": save_at(median_gap),  # honest: small when turns are fast
        "save_pct_p90": save_at(p90_gap),  # where 1h actually starts to pay
        "gap_count": int(gp[2] or 0),
        "expired_5m_count": int(gp[3] or 0),
        "near_miss_4_6m_count": int(gp[4] or 0),
        "over_60m_count": int(gp[5] or 0),
        "w5_tokens": int(vol[0]),
        "w1_tokens": int(vol[1]),
        "read_tokens": int(vol[2]),
    }


# --- per-ask cost for one session (real turns) ---
def per_ask(con, daemon=None, session_id=None, limit=28):
    """Per-turn cost within a session; amber = a turn that paid a cache write.

    Also carries `gap_min` and `expired_5m`, so the UI can show whether a turn
    probably rewrote because the previous ask fell outside the 5-minute cache TTL.
    """
    if daemon is None:
        row = con.execute(
            """
            SELECT daemon, session_id FROM ev WHERE kind='assistant_message_event'
            GROUP BY daemon, session_id HAVING count(*) BETWEEN 12 AND 60
            ORDER BY count(*) DESC LIMIT 1
            """
        ).fetchone()
        if not row:  # small corpus: fall back to the busiest assistant session
            row = con.execute(
                """
                SELECT daemon, session_id FROM ev WHERE kind='assistant_message_event'
                GROUP BY daemon, session_id ORDER BY count(*) DESC LIMIT 1
                """
            ).fetchone()
        daemon, session_id = row if row else (None, None)
    model = _daemon_model(con, daemon)
    rr = RATES.get(rate_key(model)) or RATES["claude-opus-4-8"]
    rows = con.execute(
        """
        WITH turns AS (
            SELECT ts, session_id,
                   json_extract(payload,'$.message.usage.input_tokens')::BIGINT AS inp,
                   json_extract(payload,'$.message.usage.output_tokens')::BIGINT AS out,
                   COALESCE(json_extract(payload,'$.message.usage.cache_read_input_tokens')::BIGINT,0) AS cr,
                   COALESCE(json_extract(payload,'$.message.usage.cache_creation_input_tokens')::BIGINT,0) AS cw,
                   (ts - lag(ts) OVER (PARTITION BY daemon, session_id ORDER BY ts)) / 60000.0 AS gap_min
            FROM ev WHERE kind='assistant_message_event' AND daemon = ?
              AND (? IS NULL OR session_id = ?)
        )
        SELECT session_id, inp, out, cr, cw, gap_min FROM turns ORDER BY ts LIMIT ?
        """,
        [daemon, session_id, session_id, limit],
    ).fetchall()
    out = []
    actual_sid = session_id
    for i, (sid, inp, o, cr, cw, gap_min) in enumerate(rows, 1):
        actual_sid = actual_sid or sid
        cost = (
            (inp or 0) * rr["input"]
            + (cr or 0) * rr["input"] * MULT["read"]
            + (cw or 0) * rr["input"] * MULT["write_5m"]
            + (o or 0) * rr["output"]
        ) / 1e6
        gap = round(gap_min, 2) if gap_min is not None else None
        out.append(
            {
                "i": i,
                "cost": round(cost, 4),
                "rewrite_5m": bool(cw),
                "gap_min": gap,
                "expired_5m": bool(gap is not None and gap > 5),
            }
        )
    return {"daemon": daemon, "session_id": actual_sid, "model": model, "asks": out}


def per_ask_sessions(con, n=12, asks_limit=30):
    """Per-ask cost for the top-cost sessions — the choices behind the Cost page's
    session selector. Anthropic sessions rank first (real $ + visible cache-write
    bars). Each: {id, model, cost, asks:[{i,cost,rewrite_5m}]}, costliest first.

    Keep this batched: gen_dashboard calls it every refresh, and the earlier
    implementation ran one DuckDB query per candidate session. On the live corpus
    that made this single panel dominate dashboard contract generation.
    """
    cand = con.execute(
        """
        WITH am AS (
            SELECT daemon, session_id, count(*) AS n,
                   sum(COALESCE(json_extract(payload,'$.message.usage.cache_creation_input_tokens')::BIGINT,0)) AS cwsum,
                   max(ts) AS last_ts
            FROM ev WHERE kind='assistant_message_event' GROUP BY daemon, session_id
        ),
        tel AS (
            SELECT daemon, session_id, arg_max(json_extract_string(payload,'$.model'), ts) AS model
            FROM ev WHERE kind='task_telemetry' GROUP BY daemon, session_id
        ),
        dmodel AS (
            SELECT daemon, arg_max(json_extract_string(payload,'$.model'), ts) AS model
            FROM ev WHERE kind='task_telemetry' GROUP BY daemon
        )
        SELECT am.daemon, am.session_id, COALESCE(tel.model, dmodel.model) AS model, am.last_ts
        FROM am
        LEFT JOIN tel USING (daemon, session_id)
        LEFT JOIN dmodel USING (daemon)
        WHERE am.n BETWEEN 4 AND 60
        ORDER BY (COALESCE(tel.model, dmodel.model) LIKE 'claude-%') DESC, am.cwsum DESC, am.n DESC
        LIMIT 40
        """
    ).fetchall()
    if not cand:
        return []

    con.execute(
        "CREATE OR REPLACE TEMP TABLE _per_ask_candidates(daemon VARCHAR, session_id VARCHAR)"
    )
    con.executemany(
        "INSERT INTO _per_ask_candidates VALUES (?, ?)", [(d, s) for d, s, _, _ in cand]
    )
    rows = con.execute(
        """
        WITH turns AS (
            SELECT e.daemon, e.session_id, e.ts,
                   json_extract(e.payload,'$.message.usage.input_tokens')::BIGINT AS inp,
                   json_extract(e.payload,'$.message.usage.output_tokens')::BIGINT AS out,
                   COALESCE(json_extract(e.payload,'$.message.usage.cache_read_input_tokens')::BIGINT,0) AS cr,
                   COALESCE(json_extract(e.payload,'$.message.usage.cache_creation_input_tokens')::BIGINT,0) AS cw,
                   (e.ts - lag(e.ts) OVER (PARTITION BY e.daemon, e.session_id ORDER BY e.ts)) / 60000.0 AS gap_min,
                   row_number() OVER (PARTITION BY e.daemon, e.session_id ORDER BY e.ts) AS rn
            FROM ev e
            JOIN _per_ask_candidates c USING (daemon, session_id)
            WHERE e.kind='assistant_message_event'
        )
        SELECT daemon, session_id, rn, inp, out, cr, cw, gap_min
        FROM turns WHERE rn <= ?
        ORDER BY daemon, session_id, rn
        """,
        [asks_limit],
    ).fetchall()

    by_session = {(daemon, sid): [] for daemon, sid, _model, _last_ts in cand}
    for daemon, sid, rn, inp, o, cr, cw, gap_min in rows:
        by_session[(daemon, sid)].append((rn, inp, o, cr, cw, gap_min))

    out = []
    for daemon, sid, model, last_ts in cand:
        rr = RATES.get(rate_key(model)) or RATES["claude-opus-4-8"]
        asks = []
        for rn, inp, o, cr, cw, gap_min in by_session.get((daemon, sid), []):
            cost = (
                (inp or 0) * rr["input"]
                + (cr or 0) * rr["input"] * MULT["read"]
                + (cw or 0) * rr["input"] * MULT["write_5m"]
                + (o or 0) * rr["output"]
            ) / 1e6
            gap = round(gap_min, 2) if gap_min is not None else None
            asks.append(
                {
                    "i": int(rn),
                    "cost": round(cost, 4),
                    "rewrite_5m": bool(cw),
                    "gap_min": gap,
                    "expired_5m": bool(gap is not None and gap > 5),
                }
            )
        total = round(sum(a["cost"] for a in asks), 2)
        if total > 0:
            out.append(
                {
                    "id": "mu·" + daemon[:4],
                    "session_id": _short_id("mu", f"{daemon}/{sid}"),
                    "session_ref": f"mu:{daemon}:{sid}",
                    "started": datetime.datetime.fromtimestamp(
                        (last_ts or 0) / 1000, tz=datetime.UTC
                    )
                    .date()
                    .isoformat(),
                    "model": model or "—",
                    "cost": total,
                    "asks": asks,
                }
            )
    out.sort(key=lambda s: -s["cost"])
    return out[:n]


# --- per-session transcript (the Sessions drill-down) ----------------------------
# The drill-down is the ONE place a session's whole conversation is reviewed to mark
# it, so transcripts are FULL — every turn, no clipping. Embedding all of them in the
# page is impossible (~450 MB; one session is 145 MB of multi-MB tool dumps), so each
# session is written to its own JSON sidecar (sessions/<slug>.json) and the drill-down
# fetches it on demand. write_session_transcripts only rewrites sessions whose newest
# event changed, so the hourly cron doesn't re-serialize the whole corpus every run.

# Conversational event kinds, oldest-first, that reconstruct a readable turn stream.
_TX_KINDS = ("user_message", "assistant_message_event", "tool_call", "tool_result")
# cc's sink task_id lives only on its task_telemetry event; bridge session_id -> it.
_TX_CC_TID = """
    cc_tid AS (
        SELECT session_id, any_value(json_extract_string(payload,'$.task_id')) AS tid
        FROM ev WHERE kind='task_telemetry' AND fleet='cc' GROUP BY session_id
    )"""
# The per-session key sample_data hashes into the display id: mu -> "<daemon>/<sid>",
# cc -> the task_telemetry.task_id ("cc-<uuid>"). Keep these two in lockstep.
_TX_KEY = "CASE WHEN e.fleet='mu' THEN e.daemon || '/' || e.session_id ELSE c.tid END"
# One readable body per kind. Assistant content is an array of blocks (keep the text
# ones) OR a plain string — never fall back to dumping the raw array, or a pure
# tool-use turn leaks its tool_call JSON as "text". chr(10) = newline join.
_TX_BODY = """CASE e.kind
        WHEN 'user_message' THEN json_extract_string(e.payload,'$.content')
        WHEN 'tool_result'  THEN json_extract_string(e.payload,'$.content')
        WHEN 'tool_call'    THEN CAST(json_extract(e.payload,'$.arguments') AS VARCHAR)
        WHEN 'assistant_message_event' THEN
            CASE WHEN json_type(json_extract(e.payload,'$.message.content')) = 'ARRAY'
                THEN array_to_string(list_transform(
                    from_json(json_extract(e.payload,'$.message.content'), '["json"]'),
                    x -> CASE WHEN json_extract_string(x,'$.type')='text'
                              THEN json_extract_string(x,'$.text') END), chr(10))
                ELSE json_extract_string(e.payload,'$.message.content')
            END
    END"""
_CONV_SQL = f"""
WITH{_TX_CC_TID},
conv AS (
    SELECT e.fleet, {_TX_KEY} AS key, e.ts, e.id, e.kind,
           json_extract_string(e.payload,'$.name')               AS tool_name,
           CAST(json_extract(e.payload,'$.is_error') AS BOOLEAN) AS is_error,
           {_TX_BODY} AS body
    FROM ev e
    LEFT JOIN cc_tid c ON e.fleet='cc' AND e.session_id = c.session_id
    WHERE e.kind IN {_TX_KINDS}
)"""
# Per-session signature = newest event id (event ids are append-monotonic), so an
# unchanged session has an unchanged signature and its sidecar can be skipped.
_SIG_SQL = f"""
WITH{_TX_CC_TID}
SELECT e.fleet, {_TX_KEY} AS key, max(e.id) AS max_id
FROM ev e
LEFT JOIN cc_tid c ON e.fleet='cc' AND e.session_id = c.session_id
WHERE e.kind IN {_TX_KINDS}
GROUP BY 1, 2
"""
_ALL_ROWS_SQL = (
    _CONV_SQL + "\nSELECT fleet, key, kind, tool_name, is_error, body FROM conv "
    "WHERE key IS NOT NULL ORDER BY fleet, key, ts, id"
)
_CHANGED_ROWS_SQL = (
    _CONV_SQL
    + """
SELECT c.fleet, c.key, c.kind, c.tool_name, c.is_error, c.body
FROM conv c JOIN _changed ch ON ch.fleet = c.fleet AND ch.key = c.key
WHERE c.key IS NOT NULL
ORDER BY c.fleet, c.key, c.ts, c.id"""
)


def _tx_preview(text, n=96):
    """First non-blank line of a turn body, whitespace-collapsed, clipped to n."""
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if line:
            return line[:n]
    return ""


def _turn_for_row(kind, tool_name, is_error, body):
    """One [who, preview, body] turn from an event row, or None to skip. `who` is
    'u' user / 'a' agent / 't' tool. Empty user/assistant turns are dropped (a pure
    tool-use assistant message has no text — the following tool_call carries it)."""
    body = body or ""
    if kind == "user_message":
        return ["u", _tx_preview(body), body] if body.strip() else None
    if kind == "assistant_message_event":
        return ["a", _tx_preview(body), body] if body.strip() else None
    if kind == "tool_call":
        name = tool_name or "tool"
        return ["t", _tx_preview(f"{name} · {body}" if body else name), body]
    label = "→ error" if is_error else "→ result"  # tool_result
    snip = _tx_preview(body, 64)
    return ["t", f"{label} · {snip}" if snip else label, body or "(empty result)"]


def session_transcripts(con):
    """Every session's FULL conversation, keyed by the natural key sample_data hashes
    into the display id::  { (fleet, key): [[who, preview, body], ...] }

    - mu key = "<daemon>/<session_id>"    cc key = task_telemetry.task_id ("cc-<uuid>")

    Materializes the whole corpus — fine for tests/small inputs; production writes
    sidecars via write_session_transcripts to avoid holding ~450 MB at once."""
    out: dict = {}
    for fleet, key, kind, tool_name, is_error, body in con.execute(_ALL_ROWS_SQL).fetchall():
        turn = _turn_for_row(kind, tool_name, is_error, body)
        if turn is not None:
            out.setdefault((fleet, key), []).append(turn)
    return out


def _slug(display_id):
    """Filesystem/URL-safe sidecar stem for a session display id (mu·ab12 -> mu-ab12).
    The page mirrors this exactly: fetch(`sessions/${id.replaceAll('·','-')}.json`)."""
    return display_id.replace("·", "-")


def write_session_transcripts(con, sessions_dir):
    """Write one FULL transcript per session to <sessions_dir>/<slug>.json for the
    drill-down to fetch on demand. Only sessions whose newest event id changed since
    the last run are rewritten (tracked in <sessions_dir>/_manifest.json), and sidecars
    for vanished sessions are removed. Streams row batches so peak memory is ~one
    session, not the whole corpus. Returns {"written": n, "total": n}."""
    os.makedirs(sessions_dir, exist_ok=True)
    manifest_path = os.path.join(sessions_dir, "_manifest.json")
    try:
        with open(manifest_path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}

    keymap, current = {}, {}  # slug -> (fleet, key) ; slug -> max_id
    for fleet, key, max_id in con.execute(_SIG_SQL).fetchall():
        if not key:
            continue
        slug = _slug(_short_id(fleet, key))
        keymap[slug] = (fleet, key)
        current[slug] = int(max_id)

    def path_for(slug):
        return os.path.join(sessions_dir, slug + ".json")

    changed = [
        slug
        for slug, mid in current.items()
        if old.get(slug) != mid or not os.path.exists(path_for(slug))
    ]
    for slug in old:  # drop sidecars for sessions that no longer exist
        if slug not in current:
            try:
                os.remove(path_for(slug))
            except OSError:
                pass

    if changed:
        con.execute("CREATE OR REPLACE TEMP TABLE _changed(fleet VARCHAR, key VARCHAR)")
        con.executemany("INSERT INTO _changed VALUES (?, ?)", [keymap[s] for s in changed])

        def flush(fk, turns):
            if fk is not None:
                with open(path_for(_slug(_short_id(*fk))), "w", encoding="utf-8") as f:
                    json.dump(turns, f, ensure_ascii=False, separators=(",", ":"))

        cur = con.execute(_CHANGED_ROWS_SQL)
        cur_fk, turns = None, []
        while True:
            batch = cur.fetchmany(2000)
            if not batch:
                break
            for fleet, key, kind, tool_name, is_error, body in batch:
                fk = (fleet, key)
                if fk != cur_fk:
                    flush(cur_fk, turns)
                    cur_fk, turns = fk, []
                turn = _turn_for_row(kind, tool_name, is_error, body)
                if turn is not None:
                    turns.append(turn)
        flush(cur_fk, turns)

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(current, f)
    return {"written": len(changed), "total": len(current)}


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
    tx = session_transcripts(con)
    nturns = sum(len(t) for t in tx.values())
    print(f"== session_transcripts ({len(tx)} sessions, {nturns} turns) ==")
    for k, turns in list(tx.items())[:1]:
        print(f"  {k}: {len(turns)} turns")
        print(json.dumps(turns[:6], indent=1))
