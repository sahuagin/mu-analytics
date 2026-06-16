#!/usr/bin/env python3
"""Per-turn / per-invocation model-side latency report over the unified `ev` view.

DS1 port of claude-personal/scripts/session_timings.py. The legacy walked two raw
JSONL formats with two bespoke parsers; here a single state-machine reads the typed
`ev` view (DuckDB) — both fleets, one schema. The pure aggregation core (ModelCall /
TimingStats / percentile / WorkloadFilter) is unchanged.

Per (surface, model): percentile TTFT, total duration, streaming throughput.
  - mu: provider_status_update {state:streaming, elapsed_ms} gives exact TTFT;
    each (awaiting_first_token → assistant_message_event) pair is one invocation.
  - claude-code: post-hoc log — no TTFT; one record per user→assistant round-trip.

grouping="invocation" (default): one model round-trip. grouping="turn": whole turn
(user_message → done for mu; real user_message → next for cc), invocations summed.

Run:  ./run session_timings.py [--days N] [--group-by turn] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import engine

# ── data primitives (ported verbatim from the legacy) ───────────────────────


@dataclass
class ModelCall:
    surface: str
    model_raw: str
    model_norm: str
    provider_kind: str
    session_id: str
    project: str
    ts: datetime
    ttft_ms: float | None
    duration_ms: float
    output_tokens: int
    invocation_count: int = 1


def normalize_model(model_raw: str) -> str:
    """Coarse family normalization so dated variants aggregate together."""
    if not model_raw:
        return "unknown"
    m = model_raw.lower()
    if "opus-4" in m:
        return "opus-4"
    if "sonnet-4" in m:
        return "sonnet-4"
    if "haiku-4-5" in m:
        return "haiku-4-5"
    if "haiku-4" in m:
        return "haiku-4"
    if "gpt-5.5-codex" in m or "gpt5.5-codex" in m:
        return "gpt-5.5-codex"
    if "gpt-5.5" in m or "gpt5.5" in m:
        return "gpt-5.5"
    if "gpt-5" in m:
        return "gpt-5"
    if m == "faux":
        return "faux"
    return model_raw


def percentile(sorted_vals: list[float], p: float) -> float | None:
    """Linear-interpolation percentile (numpy default, R type 7). p in 0..100."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    n = len(sorted_vals)
    k = (n - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, n - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f]))


@dataclass
class TimingStats:
    count: int = 0
    ttft_ms: list[float] = field(default_factory=list)
    duration_ms: list[float] = field(default_factory=list)
    throughput_tps: list[float] = field(default_factory=list)
    invocation_counts: list[int] = field(default_factory=list)
    output_tokens_total: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    def add(self, call: ModelCall) -> None:
        self.count += 1
        self.duration_ms.append(call.duration_ms)
        self.output_tokens_total += call.output_tokens
        self.invocation_counts.append(call.invocation_count)
        if call.ttft_ms is not None:
            self.ttft_ms.append(call.ttft_ms)
            generation_ms = call.duration_ms - call.ttft_ms
            if generation_ms > 1.0 and call.output_tokens > 0:
                self.throughput_tps.append(call.output_tokens / (generation_ms / 1000.0))
        if self.first_ts is None or call.ts < self.first_ts:
            self.first_ts = call.ts
        if self.last_ts is None or call.ts > self.last_ts:
            self.last_ts = call.ts

    def percentiles(self, which: list[float]) -> dict:
        ttft_sorted = sorted(self.ttft_ms)
        dur_sorted = sorted(self.duration_ms)
        tps_sorted = sorted(self.throughput_tps)
        return {
            "ttft_ms": {f"p{int(p)}": percentile(ttft_sorted, p) for p in which},
            "duration_ms": {f"p{int(p)}": percentile(dur_sorted, p) for p in which},
            "throughput_tps": {f"p{int(p)}": percentile(tps_sorted, p) for p in which},
        }

    @property
    def invocations_per_turn_mean(self) -> float | None:
        if not self.invocation_counts:
            return None
        return sum(self.invocation_counts) / len(self.invocation_counts)

    @property
    def invocations_per_turn_max(self) -> int | None:
        if not self.invocation_counts:
            return None
        return max(self.invocation_counts)

    def to_dict(self, percentile_set: list[float]) -> dict:
        return {
            "count": self.count,
            "output_tokens_total": self.output_tokens_total,
            "ttft_count": len(self.ttft_ms),
            "duration_count": len(self.duration_ms),
            "throughput_count": len(self.throughput_tps),
            "invocations_per_turn_mean": self.invocations_per_turn_mean,
            "invocations_per_turn_max": self.invocations_per_turn_max,
            "first_ts": self.first_ts.isoformat() if self.first_ts else None,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            **self.percentiles(percentile_set),
        }


@dataclass
class WorkloadFilter:
    max_invocations: int | None = None
    min_invocations: int | None = None
    max_duration_ms: float | None = None
    min_duration_ms: float | None = None
    max_output_tokens: int | None = None
    min_output_tokens: int | None = None

    def passes(self, call: ModelCall) -> bool:
        if self.max_invocations is not None and call.invocation_count > self.max_invocations:
            return False
        if self.min_invocations is not None and call.invocation_count < self.min_invocations:
            return False
        if self.max_duration_ms is not None and call.duration_ms > self.max_duration_ms:
            return False
        if self.min_duration_ms is not None and call.duration_ms < self.min_duration_ms:
            return False
        if self.max_output_tokens is not None and call.output_tokens > self.max_output_tokens:
            return False
        if self.min_output_tokens is not None and call.output_tokens < self.min_output_tokens:
            return False
        return True

    def is_empty(self) -> bool:
        return all(
            v is None
            for v in (
                self.max_invocations,
                self.min_invocations,
                self.max_duration_ms,
                self.min_duration_ms,
                self.max_output_tokens,
                self.min_output_tokens,
            )
        )

    def to_dict(self) -> dict:
        return {
            "max_invocations": self.max_invocations,
            "min_invocations": self.min_invocations,
            "max_duration_ms": self.max_duration_ms,
            "min_duration_ms": self.min_duration_ms,
            "max_output_tokens": self.max_output_tokens,
            "min_output_tokens": self.min_output_tokens,
        }


@dataclass
class TimingReport:
    by_surface: dict = field(default_factory=lambda: defaultdict(TimingStats))
    by_surface_model: dict = field(default_factory=lambda: defaultdict(TimingStats))
    by_day: dict = field(default_factory=lambda: defaultdict(TimingStats))
    sessions_scanned: int = 0
    grouping: str = "invocation"
    filter: WorkloadFilter = field(default_factory=WorkloadFilter)
    records_filtered_out: int = 0


_MAX_DURATION_MS = 30 * 60 * 1000  # clock-skew / interrupted-resume sanity cap
_SURFACE = {"mu": "mu", "cc": "claude-code"}


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


# ── ev-backed source: one state machine replaces four raw-JSONL parsers ──────

# Model/provider are session-level (task_telemetry) for both fleets — cc drops
# per-message model in normalization, and mu doesn't switch mid-session.
_META_SQL = """
SELECT session,
       mode(json_extract_string(payload, '$.model'))         AS model,
       mode(json_extract_string(payload, '$.provider_kind')) AS provider
FROM ev WHERE kind = 'task_telemetry' GROUP BY session
"""

# Events the state machines consume, ordered within each session.
_EVENTS_SQL = """
SELECT fleet, session, kind, ts,
       json_extract_string(payload, '$.state')                       AS state,
       COALESCE(json_extract(payload, '$.elapsed_ms')::BIGINT, 0)     AS elapsed_ms,
       COALESCE(json_extract(payload, '$.message.usage.output_tokens')::BIGINT, 0) AS out_tok
FROM ev
WHERE kind IN ('user_message', 'assistant_message_event',
               'provider_status_update', 'done', 'tool_result')
ORDER BY session, id
"""


def _ok_window(start: datetime, end: datetime, since, until) -> bool:
    if since and end < since:
        return False
    if until and start > until:
        return False
    dur = (end - start).total_seconds() * 1000.0
    return 0 <= dur <= _MAX_DURATION_MS


def _mk(surface, meta, sess, start, end, ttft, out_tok, inv, since, until):
    if not _ok_window(start, end, since, until):
        return None
    model = meta[0]
    return ModelCall(
        surface=surface,
        model_raw=model,
        model_norm=normalize_model(model),
        provider_kind=meta[1] or "",
        session_id=sess,
        project=f"{surface}/{sess.split(':')[0]}" if surface == "mu" else f"cc/{sess}",
        ts=start,
        ttft_ms=ttft,
        duration_ms=(end - start).total_seconds() * 1000.0,
        output_tokens=out_tok,
        invocation_count=inv,
    )


def _iter_session(rows, fleet, sess, meta, grouping, since, until):
    """Yield ModelCalls for one session's ordered event rows. rows: list of
    (kind, ts_ms, state, elapsed_ms, out_tok)."""
    surface = _SURFACE[fleet]
    if fleet == "mu":
        yield from _iter_mu(rows, surface, sess, meta, grouping, since, until)
    else:
        yield from _iter_cc(rows, surface, sess, meta, grouping, since, until)


def _iter_mu(rows, surface, sess, meta, grouping, since, until):
    if grouping == "turn":
        start = first_ttft = None
        out = inv = 0
        last = None
        for kind, ts_ms, state, elapsed_ms, _out in rows:
            ts = _ms_to_dt(ts_ms)
            last = ts
            if kind == "user_message":
                if start is not None and inv:
                    call = _mk(surface, meta, sess, start, ts, first_ttft, out, inv, since, until)
                    if call:
                        yield call
                start, first_ttft, out, inv = ts, None, 0, 0
            elif kind == "provider_status_update" and state == "streaming":
                if first_ttft is None and start is not None:
                    first_ttft = float(elapsed_ms or 0)
            elif kind == "assistant_message_event" and start is not None:
                inv += 1
                out += _out
            elif kind == "done" and start is not None:
                call = _mk(surface, meta, sess, start, ts, first_ttft, out, inv, since, until)
                if call and inv:
                    yield call
                start, first_ttft, out, inv = None, None, 0, 0
        if start is not None and inv and last is not None:
            call = _mk(surface, meta, sess, start, last, first_ttft, out, inv, since, until)
            if call:
                yield call
    else:  # invocation
        start = ttft = None
        for kind, ts_ms, state, elapsed_ms, out_tok in rows:
            ts = _ms_to_dt(ts_ms)
            if kind == "provider_status_update":
                if state == "awaiting_first_token":
                    start, ttft = ts, None
                elif state == "streaming" and start is not None:
                    ttft = float(elapsed_ms or 0)
            elif kind == "assistant_message_event" and start is not None:
                call = _mk(surface, meta, sess, start, ts, ttft, out_tok, 1, since, until)
                if call:
                    yield call
                start, ttft = None, None


def _iter_cc(rows, surface, sess, meta, grouping, since, until):
    if grouping == "turn":
        start = last_asst = None
        out = inv = 0
        for kind, ts_ms, _state, _elapsed, out_tok in rows:
            ts = _ms_to_dt(ts_ms)
            if kind == "user_message":
                if start is not None and last_asst is not None:
                    call = _mk(surface, meta, sess, start, last_asst, None, out, inv, since, until)
                    if call:
                        yield call
                start, last_asst, out, inv = ts, None, 0, 0
            elif kind == "assistant_message_event" and start is not None:
                inv += 1
                last_asst = ts
                out += out_tok
        if start is not None and last_asst is not None:
            call = _mk(surface, meta, sess, start, last_asst, None, out, inv, since, until)
            if call:
                yield call
    else:  # invocation: each assistant's delta from the preceding user/tool_result
        prev = None
        for kind, ts_ms, _state, _elapsed, out_tok in rows:
            ts = _ms_to_dt(ts_ms)
            if kind in ("user_message", "tool_result"):
                prev = ts
            elif kind == "assistant_message_event" and prev is not None:
                call = _mk(surface, meta, sess, prev, ts, None, out_tok, 1, since, until)
                if call:
                    yield call
                prev = None


# ── aggregation ──────────────────────────────────────────────────────────────


def compute(
    con, since=None, until=None, surfaces=None, grouping="invocation", wf=None, use_local_dates=True
) -> TimingReport:
    if grouping not in ("invocation", "turn"):
        raise ValueError(f"unknown grouping: {grouping!r}")
    if surfaces is None:
        surfaces = ["claude-code", "mu"]
    wf = wf or WorkloadFilter()
    meta = {s: (m, p) for s, m, p in con.execute(_META_SQL).fetchall()}

    r = TimingReport()
    r.grouping = grouping
    r.filter = wf

    cur_sess = cur_fleet = None
    buf: list = []

    def flush():
        if cur_sess is None or _SURFACE.get(cur_fleet) not in surfaces:
            return
        for call in _iter_session(
            buf, cur_fleet, cur_sess, meta.get(cur_sess, (None, None)), grouping, since, until
        ):
            if wf.passes(call):
                _record(r, call, use_local_dates)
            else:
                r.records_filtered_out += 1

    for fleet, session, kind, ts, state, elapsed_ms, out_tok in con.execute(_EVENTS_SQL).fetchall():
        if session != cur_sess:
            flush()
            r.sessions_scanned += 1
            cur_sess, cur_fleet, buf = session, fleet, []
        buf.append((kind, ts, state, elapsed_ms, out_tok))
    flush()
    return r


def _record(r: TimingReport, call: ModelCall, use_local_dates: bool) -> None:
    r.by_surface[call.surface].add(call)
    r.by_surface_model[(call.surface, call.model_norm)].add(call)
    day_ts = call.ts.astimezone() if use_local_dates else call.ts.astimezone(UTC)
    r.by_day[day_ts.strftime("%Y-%m-%d")].add(call)


# ── rendering ──────────────────────────────────────────────────────────────────

DEFAULT_PCTS = [50.0, 75.0, 90.0, 99.0]


def _fmt_ms(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.0f}ms" if v < 1000 else f"{v / 1000:.2f}s"


def _fmt_tps(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}"


def render_text(report: TimingReport, min_count: int = 1) -> str:
    if not report.by_surface_model:
        return "Session-timing report — no model invocations matched the window."
    unit = "turns" if report.grouping == "turn" else "model invocations"
    out = [
        f"Session-timing report ({report.grouping}-grouped) · "
        f"{report.sessions_scanned:,} sessions · "
        f"{sum(s.count for s in report.by_surface.values()):,} {unit}",
        "",
        f"{'surface':12} {'model':16} {'n':>6} {'ttftP50':>9} {'ttftP90':>9} "
        f"{'durP50':>9} {'durP90':>9} {'durP99':>9} {'tps50':>7}",
        "-" * 96,
    ]
    for surface, model in sorted(
        report.by_surface_model, key=lambda k: (k[0], -report.by_surface_model[k].count)
    ):
        s = report.by_surface_model[(surface, model)]
        if s.count < min_count:
            continue
        p = s.percentiles(DEFAULT_PCTS)
        out.append(
            f"{surface:12} {model:16} {s.count:>6,} "
            f"{_fmt_ms(p['ttft_ms']['p50']):>9} {_fmt_ms(p['ttft_ms']['p90']):>9} "
            f"{_fmt_ms(p['duration_ms']['p50']):>9} {_fmt_ms(p['duration_ms']['p90']):>9} "
            f"{_fmt_ms(p['duration_ms']['p99']):>9} {_fmt_tps(p['throughput_tps']['p50']):>7}"
        )
    out += [
        "",
        "claude-code has no TTFT (post-hoc log stores completed turns, not streaming).",
    ]
    return "\n".join(out)


def render_json(report: TimingReport, since=None, until=None, min_count: int = 1) -> str:
    pcts = DEFAULT_PCTS
    out = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "grouping": report.grouping,
        "workload_filter": report.filter.to_dict(),
        "records_filtered_out": report.records_filtered_out,
        "window": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
        "sessions_scanned": report.sessions_scanned,
        "min_count_filter": min_count,
        "percentile_set": pcts,
        "by_surface": {k: v.to_dict(pcts) for k, v in report.by_surface.items()},
        "by_surface_model": {
            f"{surf}/{model}": v.to_dict(pcts)
            for (surf, model), v in report.by_surface_model.items()
        },
        "by_day": {d: v.to_dict(pcts) for d, v in report.by_day.items()},
        "notes": {
            "ttft_availability": (
                "claude-code lacks streaming events in the post-hoc session log, so "
                "its TTFT fields are null. mu's provider_status_update streaming event "
                "carries elapsed_ms, giving exact TTFT."
            ),
        },
    }
    return json.dumps(out, indent=2, default=str)


# ── CLI ────────────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Per-turn/invocation latency over the ev view.")
    ap.add_argument("--days", type=int, default=None, help="trailing-N-day window")
    ap.add_argument(
        "--surface",
        action="append",
        choices=["claude-code", "mu"],
        help="restrict surfaces (repeatable; default both)",
    )
    ap.add_argument("--group-by", choices=["invocation", "turn"], default="invocation")
    ap.add_argument("--min-count", type=int, default=1, help="suppress small buckets")
    ap.add_argument("--max-invocations", type=int)
    ap.add_argument("--min-invocations", type=int)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args(argv)

    since = None
    if args.days:
        since = datetime.now(UTC) - timedelta(days=args.days)
    wf = WorkloadFilter(max_invocations=args.max_invocations, min_invocations=args.min_invocations)

    report = compute(
        engine.connect(),
        since=since,
        surfaces=args.surface,
        grouping=args.group_by,
        wf=wf,
    )
    if args.json:
        print(render_json(report, since=since, min_count=args.min_count))
    else:
        print(render_text(report, min_count=args.min_count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
