#!/usr/bin/env python3
"""Anthropic Admin Usage & Cost API client (DS1 port of mu_admin_usage.py).

NOT a substrate consumer — this reads the org-level Anthropic Admin API for the
authoritative billed cost the dashboard reconciles against. It's orthogonal to the
ev view (session telemetry); kept here because the dashboard's cost panel compares
our computed cost_usd to Anthropic's billed truth.

Endpoints (terrain-verified 2026-06-06 against the live org):
  GET /v1/organizations/usage_report/messages
      starting_at, ending_at (ISO-8601 Z), bucket_width=1d, group_by[]=model
      -> per-day buckets, results[] per-model token counts.
  GET /v1/organizations/cost_report
      starting_at, ending_at, bucket_width=1d  (group_by=model is REJECTED)
      -> per-day buckets; results[].amount is a USD string IN CENTS (divide by 100).

Auth: an admin key (sk-ant-admin...) sent as x-api-key. Read from ANTHROPIC_ADMIN_KEY
first, then the file at ANTHROPIC_ADMIN_KEY_FILE (default ~/.claude-personal/secrets/
anthropic-admin-key, mode 600). The key is never logged, never returned, never
interpolated into an error — failures surface only the exception class + HTTP status.

`requests` is the `admin` optional extra, imported lazily inside _get — the pure
pricing/window functions and the no-key degradation path test without it.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

_KEY_FILE = Path.home() / ".claude-personal/secrets/anthropic-admin-key"
_BASE = "https://api.anthropic.com/v1/organizations"
_VERSION = "2023-06-01"
_TIMEOUT = 30

# Per-1M USD list price by model family. Verified to reproduce cost_report to the
# cent at the day level (2026-06-06); used to attribute the authoritative per-day
# billed cost across models, since cost_report cannot group by model.
_PRICE = {
    "opus": dict(inp=5.0, out=25.0, cr=0.50, cw5=6.25, cw1=10.0),
    "sonnet": dict(inp=3.0, out=15.0, cr=0.30, cw5=3.75, cw1=6.0),
    "haiku": dict(inp=1.0, out=5.0, cr=0.10, cw5=1.25, cw1=2.0),
}


class _AdminError(Exception):
    """Carries a key-free, header-free message safe to render in the panel."""


def admin_key():
    """Admin key from env then the 600-mode file; None if neither present.
    ANTHROPIC_ADMIN_KEY_FILE overrides the default file path."""
    env = os.environ.get("ANTHROPIC_ADMIN_KEY")
    if env and env.strip():
        return env.strip()
    path = Path(os.environ.get("ANTHROPIC_ADMIN_KEY_FILE") or _KEY_FILE)
    try:
        if path.exists():
            return path.read_text().strip() or None
    except OSError:
        return None
    return None


def _family(model: str) -> str:
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    return "haiku"


def list_price_usd(rec: dict) -> float:
    """List-price USD for one usage_report per-model record (billed tokens)."""
    p = _PRICE[_family(rec.get("model", ""))]
    cw = rec.get("cache_creation") or {}
    return (
        rec.get("uncached_input_tokens", 0) / 1e6 * p["inp"]
        + rec.get("output_tokens", 0) / 1e6 * p["out"]
        + rec.get("cache_read_input_tokens", 0) / 1e6 * p["cr"]
        + cw.get("ephemeral_5m_input_tokens", 0) / 1e6 * p["cw5"]
        + cw.get("ephemeral_1h_input_tokens", 0) / 1e6 * p["cw1"]
    )


def _get(path: str, params: dict):
    """GET a report endpoint, following pagination. Returns the concatenated list of
    day-buckets, or raises _AdminError (redacted) on any failure. The key check runs
    BEFORE importing requests so the no-key path degrades without the `admin` extra."""
    key = admin_key()
    if not key:
        raise _AdminError(
            "no admin key (env ANTHROPIC_ADMIN_KEY or ~/.claude-personal/secrets/anthropic-admin-key)"
        )
    try:
        import requests  # noqa: PLC0415 — lazy: the `admin` optional extra
    except ImportError:
        raise _AdminError("requests not installed (pip install '.[admin]')") from None
    headers = {"x-api-key": key, "anthropic-version": _VERSION}
    buckets = []
    page = None
    for _ in range(50):  # hard cap; trailing-7d/1d rarely paginates
        q = dict(params)
        if page:
            q["page"] = page
        try:
            r = requests.get(f"{_BASE}/{path}", headers=headers, params=q, timeout=_TIMEOUT)
        except requests.RequestException as e:
            # `from None`: never chain — the original repr can carry the URL/headers/key
            raise _AdminError(f"{type(e).__name__} contacting {path}") from None
        if r.status_code != 200:
            raise _AdminError(f"HTTP {r.status_code} from {path}")
        try:
            body = r.json()
        except ValueError:
            raise _AdminError(f"non-JSON body from {path}") from None
        buckets.extend(body.get("data", []))
        if body.get("has_more") and body.get("next_page"):
            page = body["next_page"]
            continue
        break
    return buckets


def _utc_window(days: int, today: _dt.date | None):
    today = today or _dt.datetime.now(_dt.UTC).date()
    start = today - _dt.timedelta(days=days - 1)
    end = today + _dt.timedelta(days=1)  # include today's partial UTC bucket
    return start.strftime("%Y-%m-%dT00:00:00Z"), end.strftime("%Y-%m-%dT00:00:00Z")


def fetch_reconciliation(days: int = 7, today: _dt.date | None = None) -> dict:
    """Pull trailing-`days` billed usage + cost. Always returns a dict; on any
    failure (missing key, API error) returns {"ok": False, "note": <safe msg>} so the
    dashboard degrades to a one-line note. On success: ok, window, usage (per
    day+model), cost_by_day (authoritative, cost_report cents/100)."""
    start, end = _utc_window(days, today)
    try:
        usage_buckets = _get(
            "usage_report/messages",
            {"starting_at": start, "ending_at": end, "bucket_width": "1d", "group_by[]": "model"},
        )
        cost_buckets = _get(
            "cost_report", {"starting_at": start, "ending_at": end, "bucket_width": "1d"}
        )
    except _AdminError as e:
        return {"ok": False, "note": str(e)}

    usage = []
    for b in usage_buckets:
        day = (b.get("starting_at") or "")[:10]
        for res in b.get("results", []):
            cw = res.get("cache_creation") or {}
            usage.append(
                {
                    "day": day,
                    "model": res.get("model"),
                    "uncached_input": res.get("uncached_input_tokens", 0),
                    "cache_read": res.get("cache_read_input_tokens", 0),
                    "cache_write_5m": cw.get("ephemeral_5m_input_tokens", 0),
                    "cache_write_1h": cw.get("ephemeral_1h_input_tokens", 0),
                    "output": res.get("output_tokens", 0),
                    "billed_list_usd": round(list_price_usd(res), 4),
                }
            )

    cost_by_day = {}
    for b in cost_buckets:
        day = (b.get("starting_at") or "")[:10]
        total = 0.0
        for res in b.get("results", []):
            try:
                total += float(res.get("amount", 0)) / 100.0  # cents -> USD
            except (TypeError, ValueError):
                pass
        cost_by_day[day] = round(total, 4)

    return {"ok": True, "window": (start, end), "usage": usage, "cost_by_day": cost_by_day}


if __name__ == "__main__":
    import json

    print(json.dumps(fetch_reconciliation(), indent=2, default=str))
