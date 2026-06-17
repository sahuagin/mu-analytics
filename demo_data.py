#!/usr/bin/env python3
"""Fabricated DATA for the demo screenshot — NOT real usage.

Every number here is invented. `build()` returns the exact same contract shape
as `sample_data.build()` (see its docstring), so the dashboard renders
identically — but against fiction, so a public screenshot exposes no real spend
or session data. Used by `MU_ANALYTICS_DEMO=1 ./run gen_dashboard.py` to produce
the README screenshot.

Keep it in sync with sample_data's schema if the contract changes.
"""

import datetime
import json
import math


def _trend():
    """A plausible cost wave + a degradation hump that recovers — all synthetic."""
    start = datetime.date(2026, 4, 22)
    out = []
    for i in range(51):
        d = start + datetime.timedelta(days=i)
        cost = 120 + 90 * math.sin(i / 4.2) + 70 * math.sin(i / 11.0) + 1.6 * i
        deg = 0.04 + 0.03 * math.sin(i / 3.0)
        if 24 <= i <= 34:  # a transient "bad stretch"
            deg += 0.10 * math.sin((i - 24) / 10.0 * math.pi)
        out.append(
            {
                "date": d.isoformat(),
                "cost": round(max(35.0, cost), 2),
                "degradation": round(max(0.0, deg), 3),
            }
        )
    return out


def _demo_sessions():
    """~30 fabricated sessions spread over several days, for the Sessions page breakout."""
    models = [
        ("mu", "claude-opus-4-8", "subscription"),
        ("cc", "claude-opus-4-8", "subscription"),
        ("cc", "claude-sonnet-4-6", "subscription"),
        ("mu", "gpt-5.4", "subscription"),
        ("mu", "openrouter/deepseek-v3", "billed"),
        ("cc", "claude-haiku-4-5", "subscription"),
    ]
    outs = [
        "clean_success",
        "narrative_no_action",
        "bug_in_output",
        "hollow_commit",
        "clean_success",
    ]
    base = datetime.date(2026, 6, 14)
    out = []
    for i in range(30):
        f, m, k = models[i % len(models)]
        display_id = f"{f}·{i:03d}"
        ref = f"{f}:demo-{i:03d}"
        out.append(
            {
                "id": display_id,
                "ref": ref,
                "aliases": [display_id, ref, f"demo-{i:03d}"],
                "fleet": f,
                "model": m,
                "kind": k,
                "cost": round(0.3 + (i % 9) * 1.7, 2),
                "outcome": outs[i % len(outs)],
                "tool_calls": 8 + (i * 7) % 120,
                "started": (base - datetime.timedelta(days=i // 6)).isoformat(),
                "flagged": i % 7 == 0,
                "child": i % 11 == 0,  # a few sub-agent/branched sessions, tagged flat
            }
        )
    return out


def demo_transcripts():
    """A couple of fabricated full conversations so the demo screenshot shows the
    Sessions drill-down. Keyed by _demo_sessions() ids; each value is the turn list
    written to sessions/<slug>.json. `who` is 'u' user / 'a' agent / 't' tool."""
    return {
        "mu·000": [
            [
                "u",
                "retire the strewn analytics scripts",
                "Now that the rewrite parses the event data, fold the one-off scripts "
                "into one interface. Don't model the old pile — rebuild it.",
            ],
            [
                "a",
                "Reading the real event model first",
                "Reading event_log.rs, the forensics classifier and cost.py so the plan "
                "is grounded in what's emitted, not how the old scripts did it.",
            ],
            ["t", "bash · mu analytics compact", '{"command": "mu analytics compact"}'],
            [
                "t",
                "→ result · UPSERT 3,899 tasks into telemetry.sqlite",
                "UPSERT 3,899 tasks into telemetry.sqlite\n"
                "outcome_class: 3845 narrative_no_action · 48 error_exit",
            ],
        ],
        "cc·001": [
            [
                "u",
                "the drill-down shows the same fake convo for every session",
                "You wired the session list to real data but the conversation is a "
                "hardcoded stub. Wire it to the event log.",
            ],
            [
                "a",
                "Confirmed — the drill-down renders a module-level constant",
                "drill(s) maps a CONVO const, not anything derived from s. Reconstructing "
                "each session's real turns from the event log instead.",
            ],
            ["t", "→ result · 39 tests passed", "ruff + ty clean; 39 tests passed"],
            [
                "a",
                "Per-session transcripts now flow from the event log",
                "Each drill-down fetches its own full conversation on demand. Deep review "
                "also lives in mu-console.",
            ],
        ],
    }


def build():
    return {
        "as_of": "2026-06-12T09:14:00",
        "note": "SYNTHETIC DEMO DATA — every figure here is fabricated for illustration",
        "default_filters": {"excluded_test_sessions": 0, "excluded_test_models": []},
        "kpi": {
            "total_api_rate_equiv": 9052.40,
            "by_kind": {"subscription": 8740.00, "billed": 312.40, "free": 0.00},
        },
        # sorted by cost desc, like the real agg()
        "cost_by_kind": [
            {"label": "subscription", "sessions": 1500, "cost": 8740.00},
            {"label": "billed", "sessions": 120, "cost": 312.40},
            {"label": "free", "sessions": 264, "cost": 0.00},
        ],
        "cost_by_fleet": [
            {"label": "cc", "sessions": 1402, "cost": 6310.00},
            {"label": "mu", "sessions": 482, "cost": 2742.40},
        ],
        "cost_by_model": [
            {"fleet": "cc", "model": "claude-opus-4-8", "sessions": 712, "cost": 5120.00},
            {"fleet": "mu", "model": "claude-opus-4-8", "sessions": 208, "cost": 1640.00},
            {"fleet": "cc", "model": "claude-fable-5", "sessions": 48, "cost": 720.00},
            {"fleet": "cc", "model": "claude-sonnet-4-6", "sessions": 360, "cost": 540.00},
            {"fleet": "mu", "model": "claude-sonnet-4-6", "sessions": 240, "cost": 360.00},
            {"fleet": "mu", "model": "gpt-5.4", "sessions": 84, "cost": 210.00},
            {"fleet": "cc", "model": "claude-haiku-4-5", "sessions": 132, "cost": 96.00},
            {"fleet": "mu", "model": "openrouter/deepseek-v3", "sessions": 40, "cost": 72.40},
        ],
        "outcomes": [
            {"outcome": "clean_success", "sessions": 980},
            {"outcome": "narrative_no_action", "sessions": 520},
            {"outcome": "bug_in_output", "sessions": 180},
            {"outcome": "hollow_commit", "sessions": 96},
            {"outcome": "unclassified", "sessions": 60},
            {"outcome": "lying_state", "sessions": 48},
        ],
        # cache-read dominates, as it does in reality (~82% here)
        "cost_composition_top_session": {
            "input": 14.00,
            "output": 84.00,
            "cache_read": 612.00,
            "cache_write": 36.00,
        },
        "top_sessions": [
            # row 0 cost == sum(cost_composition_top_session) == 746.00 so the
            # exact-composition branch in the template fires on it.
            {
                "fleet": "cc",
                "model": "claude-opus-4-8",
                "kind": "subscription",
                "cost": 746.00,
                "outcome": "clean_success",
                "tool_calls": 142,
                "started": "2026-05-28",
                "flagged": False,
            },
            {
                "fleet": "mu",
                "model": "claude-opus-4-8",
                "kind": "subscription",
                "cost": 512.40,
                "outcome": "bug_in_output",
                "tool_calls": 98,
                "started": "2026-06-02",
                "flagged": False,
            },
            {
                "fleet": "cc",
                "model": "claude-fable-5",
                "kind": "subscription",
                "cost": 388.00,
                "outcome": "clean_success",
                "tool_calls": 76,
                "started": "2026-06-05",
                "flagged": False,
            },
            {
                "fleet": "cc",
                "model": "claude-opus-4-8",
                "kind": "subscription",
                "cost": 296.50,
                "outcome": "narrative_no_action",
                "tool_calls": 54,
                "started": "2026-05-30",
                "flagged": False,
            },
            {
                "fleet": "mu",
                "model": "gpt-5.4",
                "kind": "subscription",
                "cost": 210.00,
                "outcome": "clean_success",
                "tool_calls": 61,
                "started": "2026-06-08",
                "flagged": False,
            },
            {
                "fleet": "cc",
                "model": "claude-sonnet-4-6",
                "kind": "subscription",
                "cost": 142.30,
                "outcome": "hollow_commit",
                "tool_calls": 33,
                "started": "2026-06-01",
                "flagged": False,
            },
            {
                "fleet": "mu",
                "model": "openrouter/deepseek-v3",
                "kind": "billed",
                "cost": 96.40,
                "outcome": "clean_success",
                "tool_calls": 47,
                "started": "2026-06-09",
                "flagged": False,
            },
            {
                "fleet": "cc",
                "model": "claude-haiku-4-5",
                "kind": "subscription",
                "cost": 54.20,
                "outcome": "clean_success",
                "tool_calls": 28,
                "started": "2026-06-10",
                "flagged": False,
            },
        ],
        "all_sessions": _demo_sessions(),
        "session_index": {
            "by_display_id": {s["id"]: s.get("ref", s["id"]) for s in _demo_sessions()},
            "by_alias": {a: s["id"] for s in _demo_sessions() for a in s.get("aliases", [])},
        },
        "hallucination_by_model": [
            {"fleet": "cc", "model": "claude-opus-4-8", "rate": 0.061, "sessions": 712},
            {"fleet": "cc", "model": "claude-sonnet-4-6", "rate": 0.142, "sessions": 360},
            {"fleet": "mu", "model": "claude-sonnet-4-6", "rate": 0.121, "sessions": 240},
            {"fleet": "mu", "model": "claude-opus-4-8", "rate": 0.094, "sessions": 208},
            {"fleet": "cc", "model": "claude-haiku-4-5", "rate": 0.205, "sessions": 132},
            {"fleet": "mu", "model": "gpt-5.4", "rate": 0.083, "sessions": 84},
            {"fleet": "cc", "model": "claude-fable-5", "rate": 0.042, "sessions": 48},
        ],
        "trend_by_day": _trend(),
        # ── synthetic per-page slices (so MU_ANALYTICS_DEMO renders the full proto) ──
        "marks": [
            {"date": "2026-04-28", "rating": 2, "note": "degraded — looped on a missing flag"},
            {"date": "2026-05-14", "rating": 1, "note": "gaslit me about a tool"},
            {"date": "2026-05-29", "rating": 5, "note": "clean cleanroom build"},
            {"date": "2026-06-04", "rating": 2, "note": "anchored on old code"},
            {"date": "2026-06-10", "rating": 4, "note": "good recovery"},
        ],
        "flagged_queue_total": 5,
        "flagged_queue": [
            {
                "id": "mu·000",
                "session_id": "mu·000",
                "fleet": "mu",
                "model": "claude-opus-4-8",
                "reason": "deg",
                "why": "stop_reason=degraded_eof mid-task",
                "conf": "Probable 0.62",
            },
            {
                "id": "mu·1d8e",
                "fleet": "mu",
                "model": "gpt-5.5",
                "reason": "err",
                "why": "exit_reason=error",
                "conf": "Probable 0.70",
            },
            {
                "id": "mu·be07",
                "fleet": "mu",
                "model": "claude-opus-4-7",
                "reason": "callout",
                "why": "self-flag: retry refused for bash",
                "conf": "Definite 0.88",
            },
        ],
        "compaction": {
            "mu": {
                "kept": 61,
                "dropped": 22,
                "summarized": 14,
                "failed": 1,
                "before": 128400,
                "after": 41200,
                "events": 98,
            },
            "cc": {
                "kept": 0,
                "dropped": 0,
                "summarized": 0,
                "failed": 0,
                "before": 0,
                "after": 0,
                "events": 0,
            },
        },
        "context_trajectory": [
            8,
            14,
            22,
            31,
            44,
            58,
            73,
            92,
            118,
            52,
            64,
            79,
            96,
            121,
            148,
            61,
            74,
            93,
            116,
            142,
            171,
            199,
            88,
            101,
            124,
            150,
        ],
        "context_compactions": [9, 15, 22],
        "tool_mix": [
            {"tool": "bash", "count": 1840},
            {"tool": "read", "count": 1520},
            {"tool": "edit", "count": 880},
            {"tool": "grep", "count": 610},
            {"tool": "write", "count": 420},
            {"tool": "spawn_worker", "count": 96},
        ],
        "recall": [
            {"source": "ProjectFile", "items": 188, "tokens": 96200},
            {"source": "Memory", "items": 412, "tokens": 38400},
            {"source": "Bootloader", "items": 34, "tokens": 12100},
        ],
        "cache_econ": {
            "median_gap_min": 0.2,
            "p90_gap_min": 6.4,
            "save_pct": 1.0,
            "save_pct_p90": 54.0,
            "w5_tokens": 120000,
            "w1_tokens": 5800000,
            "read_tokens": 120000000,
        },
        "per_ask_sessions": [
            {
                "id": "mu·3262",
                "model": "claude-opus-4-8",
                "cost": 12.41,
                "asks": [
                    {
                        "i": i + 1,
                        "cost": round(0.45 if i % 6 == 0 else 0.05 + (i % 5) * 0.01, 3),
                        "rewrite_5m": i % 6 == 0,
                    }
                    for i in range(28)
                ],
            },
            {
                "id": "mu·8c78",
                "model": "claude-sonnet-4-6",
                "cost": 3.13,
                "asks": [
                    {"i": i + 1, "cost": round(0.02 + (i % 4) * 0.006, 3), "rewrite_5m": i == 0}
                    for i in range(20)
                ],
            },
        ],
        "stop_reason_health": [
            {"stop_reason": "end_turn", "count": 4492},
            {"stop_reason": "iteration_cap", "count": 66},
            {"stop_reason": "max_tokens", "count": 50},
        ],
        "degradation_rate": 3.5,
        # SYNTHETIC ML-degradation probe + mu-audit findings (the fold's contract).
        "degradation_probe": {
            "r2": 0.18,
            "mae": 22.4,
            "n_interactive": 351,
            "n_unattended": 316,
            "importances": [["input_tok", 0.44], ["wall_p95", 0.38], ["tool_calls", 0.14]],
            "unnoticed": [
                {
                    "ref": "mu·a1b2c3d4",
                    "window": "wd-jun15",
                    "started": "2026-06-15T13:00",
                    "obs": -10.0,
                    "pred": 35.0,
                    "resid": 45.0,
                    "n_user": 8,
                    "pos": 0,
                    "neg": 2,
                    "net": -2,
                    "ending": "abrupt",
                    "calls": 40,
                    "tool_calls": 120,
                    "cost": 4.20,
                },
            ],
            "task_frust": [
                {
                    "ref": "cc·e5f6a7b8",
                    "window": "wd-jun15",
                    "started": "2026-06-15T09:00",
                    "obs": 60.0,
                    "pred": -5.0,
                    "resid": -65.0,
                    "n_user": 12,
                    "pos": 6,
                    "neg": 0,
                    "net": 6,
                    "ending": "signoff",
                    "calls": 22,
                    "tool_calls": 18,
                    "cost": 1.10,
                },
            ],
            "unattended": [
                {
                    "ref": "mu·c9d0e1f2",
                    "window": "W-jun13",
                    "started": "2026-06-13T22:00",
                    "pred": -40.0,
                    "calls": 60,
                    "tool_calls": 210,
                    "cost": 8.80,
                },
            ],
        },
        "audit_findings": [
            {
                "ref": "mu·a1b2c3d4",
                "first_ts": "2026-06-15T13:00",
                "severity": "High",
                "invariant": "repeated_identical_tool_call",
                "event_id": "452",
                "detail": "tool `write` called 3x with identical arguments",
            },
        ],
        # SYNTHETIC worker-orchestration slice for the Delegations page.
        "delegations": {
            "orchestrators": 3,
            "workers": [
                {
                    "session_ref": "mu:a1b2c3d4:session-1",
                    "pot": "mu-worker-session-1",
                    "model": "claude-opus-4-8",
                    "prompt": "Find the dashboard scripts and report back.",
                    "started": "2026-06-15T22:10",
                    "outcome": "exited",
                    "detail": "exit 0",
                    "elapsed_ms": 28765,
                    "mailbox": 4,
                },
                {
                    "session_ref": "mu:e5f6a7b8:session-1",
                    "pot": "mu-worker-session-2",
                    "model": "gpt-5.5",
                    "prompt": "Port the timings parser onto the ev view.",
                    "started": "2026-06-15T20:02",
                    "outcome": "failed",
                    "detail": "exit code 4",
                    "elapsed_ms": None,
                    "mailbox": 2,
                },
                {
                    "session_ref": "mu:c9d0e1f2:session-1",
                    "pot": "mu-worker-session-3",
                    "model": "claude-opus-4-8",
                    "prompt": "Long sweep over the corpus.",
                    "started": "2026-06-15T18:40",
                    "outcome": "timeout",
                    "detail": "timed out",
                    "elapsed_ms": 123490,
                    "mailbox": 1,
                },
            ],
            "by_outcome": [
                {"outcome": "exited", "n": 1},
                {"outcome": "failed", "n": 1},
                {"outcome": "timeout", "n": 1},
            ],
            "mailbox": {
                "posted": 5,
                "consumed": 2,
                "by_kind": [{"kind": "task", "n": 3}, {"kind": "result", "n": 2}],
            },
        },
        "meta": {
            "enrichment_status": "pending_commit_enricher",
            "duckdb": True,
            "event_dir_present": True,
            "marks_n": 5,
            "flags": {
                "overview": {"thin": False},
                "cost": {"thin": False, "cache_tier_sparse": True},
                "sessions": {"thin": False},
                "behavioral": {
                    "thin": True,
                    "cc_behavioral_empty": True,
                    "reason": "cc bridge emits no stop_reason/tool_result yet",
                },
                "internalops": {
                    "fleetScope": "mu",
                    "thin": True,
                    "compaction_actions_partial": True,
                },
            },
        },
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
