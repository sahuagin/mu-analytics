#!/usr/bin/env python3
"""Bounded, resumable pointwise judging over one explicitly named session.

The existing ``engine.ev`` projection is the only input parser.  This program
selects one session, renders its conversational events into stable numbered
turns, packs bounded chunks, and commits each successful chunk verdict before
advancing.  It deliberately does not enumerate sessions or fall through to a
second provider.

Example (operator chooses the trusted provider alias explicitly)::

    ./run pointwise_judge.py --fleet mu --session daemon:session-1 \
        --cls false_success --required-provider flashnext

Development and CI use synthetic ``--events-glob`` inputs.  Never use a remote
parent agent to run this over confidential sessions: verdicts and evidence are
derived from the transcript too.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence

import engine
import panels

HERE = os.path.dirname(os.path.abspath(__file__))
JUDGE = os.path.join(HERE, "behavior-judge", "scripts", "run_judge.py")
PROMPT = os.path.join(HERE, "behavior-judge", "judge", "behavior-judge-system-prompt.txt")
RUBRIC = os.path.join(HERE, "behavior-judge", "judge", "rubric.md")
DEFAULT_STORE = os.path.join(HERE, "data", "pointwise.sqlite")
PIPELINE_VERSION = "pointwise-v2"
POINTWISE_CLASSES = (
    "false_success",
    "map_as_terrain",
    "scope_overreach",
    "dismissiveness",
    "outcome_prediction",
)
FINAL_CHUNK_CLASSES = ("performative_closing",)
SESSION_WIDE_CLASSES = ("relitigation", "rule_echo")


@dataclasses.dataclass(frozen=True)
class Turn:
    event_id: int
    number: int
    kind: str
    tool_name: str | None
    is_error: bool | None
    body: str


@dataclasses.dataclass(frozen=True)
class Chunk:
    index: int
    first_event_id: int
    last_event_id: int
    text: str
    content_hash: str


# This uses the normalized ``ev.session`` key directly.  It deliberately does
# not use the dashboard's display-id aliases: callers identify exactly one
# engine session and the database boundary returns no rows from another.
_ONE_SESSION_SQL = f"""
SELECT e.id, e.kind,
       json_extract_string(e.payload,'$.name')               AS tool_name,
       CAST(json_extract(e.payload,'$.is_error') AS BOOLEAN) AS is_error,
       {panels._TX_BODY} AS body
FROM ev e
WHERE e.fleet = ? AND e.session = ? AND e.kind IN {panels._TX_KINDS}
ORDER BY e.ts, e.id
"""


def load_turns(con, fleet: str, session_key: str) -> list[Turn]:
    rows = con.execute(_ONE_SESSION_SQL, [fleet, session_key]).fetchall()
    turns = []
    for event_id, kind, tool_name, is_error, body in rows:
        body = body or ""
        if kind in ("user_message", "assistant_message_event") and not body.strip():
            continue
        turns.append(
            Turn(
                event_id=int(event_id),
                number=len(turns) + 1,
                kind=kind,
                tool_name=tool_name,
                is_error=is_error,
                body=body,
            )
        )
    return turns


def _render_turn(turn: Turn, max_tool_chars: int) -> str:
    if turn.kind == "user_message":
        label = "USER"
        body = turn.body
    elif turn.kind == "assistant_message_event":
        label = "ASSISTANT"
        body = turn.body
    elif turn.kind == "tool_call":
        label = f"TOOL_CALL({turn.tool_name or 'tool'})"
        body = turn.body[:max_tool_chars]
    else:
        label = f"TOOL_RESULT({'err' if turn.is_error else 'ok'})"
        body = (turn.body or "(empty result)")[:max_tool_chars]
    return f"[{turn.number:03}] {label}: {body}"


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _split_oversize(rendered: str, limit: int) -> list[str]:
    """Split one exceptional oversized turn without breaking UTF-8."""
    parts = []
    rest = rendered
    while rest:
        raw = rest.encode("utf-8")
        if len(raw) <= limit:
            parts.append(rest)
            break
        cut = raw[:limit]
        while cut:
            try:
                text = cut.decode("utf-8")
                break
            except UnicodeDecodeError as e:
                cut = cut[: e.start]
        if not cut:
            raise ValueError("chunk byte limit is too small for one UTF-8 character")
        parts.append(text)
        rest = rest[len(text) :]
    return parts


def chunk_turns(
    turns: Sequence[Turn], max_chunk_bytes: int, max_tool_chars: int = 1200
) -> list[Chunk]:
    """Pack complete turns when possible; only an oversized turn is split."""
    if max_chunk_bytes < 64:
        raise ValueError("max_chunk_bytes must be at least 64")
    pieces: list[tuple[int, str]] = []
    for turn in turns:
        rendered = _render_turn(turn, max_tool_chars)
        for part_no, part in enumerate(_split_oversize(rendered, max_chunk_bytes), 1):
            if part_no > 1:
                marker = f"[continuation of turn {turn.number}] "
                room = max_chunk_bytes - _utf8_len(marker)
                if room < 1:
                    raise ValueError("max_chunk_bytes leaves no room after continuation marker")
                # Re-split if the marker made this part too large.
                subparts = _split_oversize(part, room)
                pieces.extend((turn.event_id, marker + sub) for sub in subparts)
            else:
                pieces.append((turn.event_id, part))

    packed: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    size = 0
    for event_id, text in pieces:
        cost = _utf8_len(text) + (1 if current else 0)
        if current and size + cost > max_chunk_bytes:
            packed.append(current)
            current, size = [], 0
            cost = _utf8_len(text)
        current.append((event_id, text))
        size += cost
    if current:
        packed.append(current)

    out = []
    for index, rows in enumerate(packed):
        text = "\n".join(row[1] for row in rows)
        if _utf8_len(text) > max_chunk_bytes:
            raise AssertionError("chunker exceeded its byte bound")
        out.append(
            Chunk(
                index=index,
                first_event_id=rows[0][0],
                last_event_id=rows[-1][0],
                text=text,
                content_hash=hashlib.blake2b(text.encode(), digest_size=16).hexdigest(),
            )
        )
    return out


def analyzer_version(cls: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(PIPELINE_VERSION.encode())
    for path in (PROMPT, RUBRIC):
        with open(path, "rb") as f:
            h.update(f.read())
    h.update(cls.encode())
    return h.hexdigest()


def source_version(turns: Sequence[Turn]) -> str:
    h = hashlib.blake2b(digest_size=16)
    for t in turns:
        h.update(json.dumps(dataclasses.asdict(t), sort_keys=True).encode())
        h.update(b"\n")
    return h.hexdigest()


class ResultStore:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS pointwise_result (
                session_ref TEXT NOT NULL,
                source_version TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                first_event_id INTEGER NOT NULL,
                last_event_id INTEGER NOT NULL,
                chunk_hash TEXT NOT NULL,
                analyzer_id TEXT NOT NULL,
                analyzer_version TEXT NOT NULL,
                target TEXT NOT NULL,
                processed_at INTEGER NOT NULL,
                verdict_json TEXT NOT NULL,
                PRIMARY KEY (
                    session_ref, chunk_index, first_event_id, last_event_id, chunk_hash,
                    analyzer_id, analyzer_version, target
                )
            )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS pointwise_quarantine (
                session_ref TEXT NOT NULL,
                source_version TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                first_event_id INTEGER NOT NULL,
                last_event_id INTEGER NOT NULL,
                chunk_hash TEXT NOT NULL,
                analyzer_id TEXT NOT NULL,
                analyzer_version TEXT NOT NULL,
                target TEXT NOT NULL,
                quarantined_at INTEGER NOT NULL,
                category TEXT NOT NULL,
                reason_json TEXT NOT NULL,
                initial_verdict_json TEXT NOT NULL,
                retry_verdict_json TEXT NOT NULL,
                egress_allowed INTEGER NOT NULL DEFAULT 0 CHECK (egress_allowed IN (0,1)),
                resolution TEXT,
                resolved_at INTEGER,
                PRIMARY KEY (
                    session_ref, chunk_index, first_event_id, last_event_id, chunk_hash,
                    analyzer_id, analyzer_version, target
                )
            )"""
        )
        self.db.commit()

    @staticmethod
    def _key_values(session_ref, chunk, cls, version, target):
        return [
            session_ref,
            chunk.index,
            chunk.first_event_id,
            chunk.last_event_id,
            chunk.content_hash,
            cls,
            version,
            target,
        ]

    def status(self, session_ref: str, chunk: Chunk, cls: str, version: str, target: str):
        key = self._key_values(session_ref, chunk, cls, version, target)
        if self.db.execute(
            """SELECT 1 FROM pointwise_result
               WHERE session_ref=? AND chunk_index=? AND first_event_id=? AND last_event_id=?
                 AND chunk_hash=? AND analyzer_id=? AND analyzer_version=? AND target=?""",
            key,
        ).fetchone():
            return "accepted"
        if self.db.execute(
            """SELECT 1 FROM pointwise_quarantine
               WHERE session_ref=? AND chunk_index=? AND first_event_id=? AND last_event_id=?
                 AND chunk_hash=? AND analyzer_id=? AND analyzer_version=? AND target=?""",
            key,
        ).fetchone():
            return "quarantined"
        return None

    def contains(self, session_ref: str, chunk: Chunk, cls: str, version: str, target: str) -> bool:
        return self.status(session_ref, chunk, cls, version, target) is not None

    def record(
        self,
        session_ref: str,
        src_version: str,
        chunk: Chunk,
        cls: str,
        version: str,
        target: str,
        verdict: dict,
    ) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO pointwise_result VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [
                session_ref,
                src_version,
                chunk.index,
                chunk.first_event_id,
                chunk.last_event_id,
                chunk.content_hash,
                cls,
                version,
                target,
                int(time.time()),
                json.dumps(verdict, sort_keys=True),
            ],
        )
        self.db.commit()  # each successful chunk survives a later model failure

    def quarantine(
        self,
        session_ref: str,
        src_version: str,
        chunk: Chunk,
        cls: str,
        version: str,
        target: str,
        category: str,
        reason: dict,
        initial: dict,
        retry: dict,
    ) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO pointwise_quarantine
               (session_ref, source_version, chunk_index, first_event_id, last_event_id,
                chunk_hash, analyzer_id, analyzer_version, target, quarantined_at,
                category, reason_json, initial_verdict_json, retry_verdict_json,
                egress_allowed, resolution, resolved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL)""",
            [
                session_ref,
                src_version,
                chunk.index,
                chunk.first_event_id,
                chunk.last_event_id,
                chunk.content_hash,
                cls,
                version,
                target,
                int(time.time()),
                category,
                json.dumps(reason, sort_keys=True),
                json.dumps(initial, sort_keys=True),
                json.dumps(retry, sort_keys=True),
            ],
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()


def resolve_target(role: str, required_provider: str) -> str:
    r = subprocess.run(["agent-role", role, "0"], capture_output=True, text=True, timeout=20)
    fields = r.stdout.strip().split()
    if r.returncode or len(fields) < 2:
        raise RuntimeError(f"role {role!r} rank 0 did not resolve")
    provider, model = fields[:2]
    if provider != required_provider:
        raise RuntimeError(
            f"role {role!r} rank 0 is {provider!r}, required {required_provider!r}; refusing dispatch"
        )
    return f"{provider}/{model}"


def judge_chunk(
    chunk: Chunk,
    cls: str,
    role: str,
    required_provider: str,
    timeout: int,
    verification_retry: bool = False,
) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(chunk.text)
        path = f.name
    try:
        cmd = [
            sys.executable,
            JUDGE,
            "--transcript",
            path,
            "--cls",
            cls,
            "--role",
            role,
            "--single-rank",
            "--require-provider",
            required_provider,
            "--rubric-at-tail",
            "--timeout",
            str(timeout),
        ]
        if verification_retry:
            cmd.append("--verification-retry")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 90)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout or "judge failed").strip().splitlines()[-1])
        verdict = json.loads(r.stdout)
        if not isinstance(verdict.get("occurred"), bool):
            raise RuntimeError("judge returned no boolean verdict")
        return verdict
    finally:
        os.unlink(path)


def evidence_counts(verdict: dict) -> tuple[int, int]:
    claimed = len(verdict.get("evidence") or [])
    verified = int(verdict.get("n_evidence_verified") or 0)
    return claimed, verified


def positive_evidence_verified(verdict: dict) -> bool:
    claimed, verified = evidence_counts(verdict)
    return verdict.get("occurred") is not True or (claimed > 0 and verified == claimed)


def classes_for_chunk(index: int, n_chunks: int, profile: bool, single_cls: str):
    if not profile:
        return (single_cls,)
    classes = list(POINTWISE_CLASSES)
    if index == n_chunks - 1:
        classes.extend(FINAL_CHUNK_CLASSES)
    return tuple(classes)


def session_plan(con, fleet: str, session_key: str, max_chunk_bytes: int, max_tool_chars: int):
    """Return the bounded work shape without transcript text or model access."""
    turns = load_turns(con, fleet, session_key)
    if not turns:
        raise RuntimeError(f"no conversational events for {fleet}:{session_key}")
    chunks = chunk_turns(turns, max_chunk_bytes, max_tool_chars)
    sizes = [_utf8_len(c.text) for c in chunks]
    return (
        turns,
        chunks,
        {
            "session_ref": f"{fleet}:{session_key}",
            "turns": len(turns),
            "chunks": len(chunks),
            "chunk_bytes_total": sum(sizes),
            "chunk_bytes_min": min(sizes),
            "chunk_bytes_max": max(sizes),
        },
    )


def process_session(
    con,
    fleet: str,
    session_key: str,
    cls: str,
    required_provider: str,
    store: ResultStore,
    max_chunk_bytes: int,
    max_tool_chars: int,
    timeout: int,
    limit_chunks: int = 0,
    role: str = "judge",
    judge: Callable[..., dict] = judge_chunk,
    target_resolver: Callable[[str, str], str] = resolve_target,
    pointwise_profile: bool = False,
) -> dict:
    turns, chunks, plan = session_plan(con, fleet, session_key, max_chunk_bytes, max_tool_chars)
    src_version = source_version(turns)
    target = target_resolver(role, required_provider)
    session_ref = f"{fleet}:{session_key}"
    versions = {
        name: analyzer_version(name)
        for chunk in chunks
        for name in classes_for_chunk(chunk.index, len(chunks), pointwise_profile, cls)
    }

    terminal = {}
    pending_by_chunk = {}
    for chunk in chunks:
        pending = []
        for name in classes_for_chunk(chunk.index, len(chunks), pointwise_profile, cls):
            state = store.status(session_ref, chunk, name, versions[name], target)
            terminal[(chunk.index, name)] = state
            if state is None:
                pending.append(name)
        if pending:
            pending_by_chunk[chunk.index] = pending

    pending_chunks = [c for c in chunks if c.index in pending_by_chunk]
    selected = pending_chunks[:limit_chunks] if limit_chunks > 0 else pending_chunks
    accepted = quarantined = model_calls = 0
    for chunk in selected:
        for name in pending_by_chunk[chunk.index]:
            initial = judge(chunk, name, role, required_provider, timeout, False)
            model_calls += 1
            if initial.get("judge_model") and initial["judge_model"] != target:
                raise RuntimeError(
                    f"judge reported target {initial['judge_model']!r}, expected {target!r}"
                )

            initial_claimed, initial_verified = evidence_counts(initial)
            if initial.get("occurred") is True and not positive_evidence_verified(initial):
                retry = judge(chunk, name, role, required_provider, timeout, True)
                model_calls += 1
                if retry.get("judge_model") and retry["judge_model"] != target:
                    raise RuntimeError(
                        f"judge reported target {retry['judge_model']!r}, expected {target!r}"
                    )
                retry_claimed, retry_verified = evidence_counts(retry)
                if not positive_evidence_verified(retry):
                    store.quarantine(
                        session_ref,
                        src_version,
                        chunk,
                        name,
                        versions[name],
                        target,
                        "positive_unverified_evidence_after_retry",
                        {
                            "initial_claimed": initial_claimed,
                            "initial_verified": initial_verified,
                            "retry_claimed": retry_claimed,
                            "retry_verified": retry_verified,
                            "egress_default": "deny",
                        },
                        initial,
                        retry,
                    )
                    quarantined += 1
                    continue
                retry["verification_disposition"] = "accepted_after_retry"
                retry["verification_attempts"] = 2
                retry["initial_unverified_count"] = initial_claimed - initial_verified
                verdict = retry
            else:
                verdict = initial
                verdict["verification_attempts"] = 1
                verdict["verification_disposition"] = (
                    "negative_with_unverified_evidence"
                    if initial.get("occurred") is False and initial_verified < initial_claimed
                    else "accepted"
                )

            store.record(
                session_ref,
                src_version,
                chunk,
                name,
                versions[name],
                target,
                verdict,
            )
            accepted += 1

    completed_before = sum(state is not None for state in terminal.values())
    quarantined_before = sum(state == "quarantined" for state in terminal.values())
    units_total = len(terminal)
    processed = accepted + quarantined
    return {
        **plan,
        "analyzers": len(versions),
        "units_total": units_total,
        "completed_before": completed_before,
        "quarantined_before": quarantined_before,
        "pending_before": units_total - completed_before,
        "accepted": accepted,
        "quarantined": quarantined,
        "model_calls": model_calls,
        "remaining": units_total - completed_before - processed,
        "target": target,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fleet", choices=("mu", "cc"), required=True)
    ap.add_argument("--session", required=True, help="exact normalized session key")
    ap.add_argument("--cls", default="false_success", help="one rubric class")
    ap.add_argument(
        "--pointwise-profile",
        action="store_true",
        help="run approved pointwise classes per chunk and performative_closing on the final chunk",
    )
    ap.add_argument(
        "--required-provider", help="trusted provider alias; fail closed (required to judge)"
    )
    ap.add_argument("--role", default="judge")
    ap.add_argument("--max-chunk-bytes", type=int, default=65536)
    ap.add_argument("--max-tool-chars", type=int, default=1200)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--limit-chunks", type=int, default=0, help="judge at most N pending chunks")
    ap.add_argument(
        "--plan-only",
        action="store_true",
        help="print aggregate chunk metadata; do not resolve a provider or open a result store",
    )
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument(
        "--events-glob", help="explicit synthetic/test source instead of production snapshot"
    )
    args = ap.parse_args()

    con = (
        engine.connect(glob=args.events_glob, fleet=args.fleet)
        if args.events_glob
        else engine.connect()
    )
    if args.plan_only:
        _turns, _chunks, plan = session_plan(
            con, args.fleet, args.session, args.max_chunk_bytes, args.max_tool_chars
        )
        if args.pointwise_profile:
            plan.update(
                {
                    "pointwise_analyzers": list(POINTWISE_CLASSES),
                    "final_chunk_analyzers": list(FINAL_CHUNK_CLASSES),
                    "deferred_session_wide": list(SESSION_WIDE_CLASSES),
                    "units_total": len(_chunks) * len(POINTWISE_CLASSES) + len(FINAL_CHUNK_CLASSES),
                }
            )
        print(json.dumps(plan, sort_keys=True))
        return
    if not args.required_provider:
        ap.error("--required-provider is required unless --plan-only is used")

    store = ResultStore(os.path.expanduser(args.store))
    try:
        stats = process_session(
            con,
            args.fleet,
            args.session,
            args.cls,
            args.required_provider,
            store,
            args.max_chunk_bytes,
            args.max_tool_chars,
            args.timeout,
            args.limit_chunks,
            args.role,
            pointwise_profile=args.pointwise_profile,
        )
    finally:
        store.close()
    print(json.dumps(stats, sort_keys=True))


if __name__ == "__main__":
    main()
