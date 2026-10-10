#!/usr/bin/env python3
"""Run the local pointwise profile for an explicit allowlist of sessions.

This driver never enumerates the corpus.  The operator supplies exact normalized
``engine.ev`` session keys, and every model dispatch remains pinned to one
required provider with rank fallthrough disabled by ``pointwise_judge``.
Only aggregate progress is printed; transcript and verdict text remain local.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from collections.abc import Callable, Sequence

import engine
import pointwise_judge as pointwise


@dataclasses.dataclass(frozen=True)
class AllowedSession:
    fleet: str
    session: str

    @property
    def ref(self) -> str:
        return f"{self.fleet}:{self.session}"


def load_allowlist(path: str) -> list[AllowedSession]:
    with open(os.path.expanduser(path)) as f:
        raw = json.load(f)
    if not isinstance(raw, dict) or set(raw) != {"sessions"}:
        raise ValueError("allowlist must be an object containing only 'sessions'")
    if not isinstance(raw["sessions"], list):
        raise ValueError("allowlist 'sessions' must be a list")

    sessions = []
    seen = set()
    for i, item in enumerate(raw["sessions"]):
        if not isinstance(item, dict) or set(item) != {"fleet", "session"}:
            raise ValueError(f"sessions[{i}] must contain exactly fleet and session")
        fleet, session = item["fleet"], item["session"]
        if fleet not in ("mu", "cc") or not isinstance(session, str) or not session.strip():
            raise ValueError(f"sessions[{i}] has an invalid fleet or session")
        key = (fleet, session)
        if key in seen:
            raise ValueError(f"duplicate allowlist session {fleet}:{session}")
        seen.add(key)
        sessions.append(AllowedSession(fleet, session))
    if not sessions:
        raise ValueError("allowlist has no sessions")
    return sessions


def run_allowlist(
    con,
    sessions: Sequence[AllowedSession],
    required_provider: str,
    store: pointwise.ResultStore,
    max_chunk_bytes: int,
    max_tool_chars: int,
    timeout: int,
    limit_chunks: int,
    role: str = "judge",
    processor: Callable[..., dict] = pointwise.process_session,
    target_resolver: Callable[[str, str], str] = pointwise.resolve_target,
) -> tuple[list[dict], int]:
    # Resolve once so one run cannot silently change routes between sessions.
    target = target_resolver(role, required_provider)

    def pinned_target(_role: str, provider: str) -> str:
        if provider != required_provider:
            raise RuntimeError("required provider changed inside the driver")
        return target

    rows = []
    failures = 0
    for selected in sessions:
        try:
            stats = processor(
                con,
                selected.fleet,
                selected.session,
                "false_success",  # ignored by the approved profile
                required_provider,
                store,
                max_chunk_bytes,
                max_tool_chars,
                timeout,
                limit_chunks,
                role,
                target_resolver=pinned_target,
                pointwise_profile=True,
            )
            rows.append({"session_ref": selected.ref, "status": "ok", **stats})
        except Exception as exc:
            # Exception text can contain provider output derived from a transcript.
            # Keep stdout/stderr aggregate-only; detailed local diagnostics remain
            # in provider/session logs and are retrieved deliberately when needed.
            failures += 1
            rows.append(
                {
                    "session_ref": selected.ref,
                    "status": "error",
                    "error_type": type(exc).__name__,
                }
            )
    return rows, failures


def plan_allowlist(
    con, sessions: Sequence[AllowedSession], max_chunk_bytes: int, max_tool_chars: int
):
    rows = []
    failures = 0
    for selected in sessions:
        try:
            _turns, chunks, stats = pointwise.session_plan(
                con, selected.fleet, selected.session, max_chunk_bytes, max_tool_chars
            )
            stats.update(
                {
                    "status": "planned",
                    "pointwise_analyzers": list(pointwise.POINTWISE_CLASSES),
                    "final_chunk_analyzers": list(pointwise.FINAL_CHUNK_CLASSES),
                    "deferred_session_wide": list(pointwise.SESSION_WIDE_CLASSES),
                    "units_total": len(chunks) * len(pointwise.POINTWISE_CLASSES)
                    + len(pointwise.FINAL_CHUNK_CLASSES),
                }
            )
            rows.append(stats)
        except Exception as exc:
            failures += 1
            rows.append(
                {
                    "session_ref": selected.ref,
                    "status": "error",
                    "error_type": type(exc).__name__,
                }
            )
    return rows, failures


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allowlist", required=True, help="local JSON file of exact session keys")
    ap.add_argument(
        "--required-provider", help="trusted provider alias; required unless --plan-only"
    )
    ap.add_argument("--role", default="judge")
    ap.add_argument("--store", default=pointwise.DEFAULT_STORE)
    ap.add_argument("--max-chunk-bytes", type=int, default=65536)
    ap.add_argument("--max-tool-chars", type=int, default=1200)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--limit-chunks", type=int, default=0, help="per-session chunk limit")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument(
        "--events-glob",
        help="explicit synthetic/test source; every allowlisted session must use the same fleet",
    )
    args = ap.parse_args()

    sessions = load_allowlist(args.allowlist)
    if args.events_glob:
        fleets = {s.fleet for s in sessions}
        if len(fleets) != 1:
            ap.error("--events-glob requires an allowlist containing one fleet")
        con = engine.connect(glob=args.events_glob, fleet=next(iter(fleets)))
    else:
        con = engine.connect()

    if args.plan_only:
        rows, failures = plan_allowlist(con, sessions, args.max_chunk_bytes, args.max_tool_chars)
    else:
        if not args.required_provider:
            ap.error("--required-provider is required unless --plan-only is used")
        store = pointwise.ResultStore(os.path.expanduser(args.store))
        try:
            rows, failures = run_allowlist(
                con,
                sessions,
                args.required_provider,
                store,
                args.max_chunk_bytes,
                args.max_tool_chars,
                args.timeout,
                args.limit_chunks,
                args.role,
            )
        finally:
            store.close()

    print(json.dumps({"sessions": rows, "failures": failures}, sort_keys=True))
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
