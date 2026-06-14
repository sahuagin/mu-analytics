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
        if 24 <= i <= 34:                      # a transient "bad stretch"
            deg += 0.10 * math.sin((i - 24) / 10.0 * math.pi)
        out.append({"date": d.isoformat(),
                    "cost": round(max(35.0, cost), 2),
                    "degradation": round(max(0.0, deg), 3)})
    return out


def build():
    return {
        "as_of": "2026-06-12T09:14:00",
        "note": "SYNTHETIC DEMO DATA — every figure here is fabricated for illustration",
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
            "input": 14.00, "output": 84.00, "cache_read": 612.00, "cache_write": 36.00,
        },
        "top_sessions": [
            # row 0 cost == sum(cost_composition_top_session) == 746.00 so the
            # exact-composition branch in the template fires on it.
            {"fleet": "cc", "model": "claude-opus-4-8", "kind": "subscription", "cost": 746.00,
             "outcome": "clean_success", "tool_calls": 142, "started": "2026-05-28", "flagged": False},
            {"fleet": "mu", "model": "claude-opus-4-8", "kind": "subscription", "cost": 512.40,
             "outcome": "bug_in_output", "tool_calls": 98, "started": "2026-06-02", "flagged": False},
            {"fleet": "cc", "model": "claude-fable-5", "kind": "subscription", "cost": 388.00,
             "outcome": "clean_success", "tool_calls": 76, "started": "2026-06-05", "flagged": False},
            {"fleet": "cc", "model": "claude-opus-4-8", "kind": "subscription", "cost": 296.50,
             "outcome": "narrative_no_action", "tool_calls": 54, "started": "2026-05-30", "flagged": False},
            {"fleet": "mu", "model": "gpt-5.4", "kind": "subscription", "cost": 210.00,
             "outcome": "clean_success", "tool_calls": 61, "started": "2026-06-08", "flagged": False},
            {"fleet": "cc", "model": "claude-sonnet-4-6", "kind": "subscription", "cost": 142.30,
             "outcome": "hollow_commit", "tool_calls": 33, "started": "2026-06-01", "flagged": False},
            {"fleet": "mu", "model": "openrouter/deepseek-v3", "kind": "billed", "cost": 96.40,
             "outcome": "clean_success", "tool_calls": 47, "started": "2026-06-09", "flagged": False},
            {"fleet": "cc", "model": "claude-haiku-4-5", "kind": "subscription", "cost": 54.20,
             "outcome": "clean_success", "tool_calls": 28, "started": "2026-06-10", "flagged": False},
        ],
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
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
