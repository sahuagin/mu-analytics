#!/usr/bin/env python3
"""Emit the FULL mu-core SessionEvent stream from claude-code (cc) session logs.

WS2 of the cc full-fidelity event unification (bead mu-cc-event-unification-lkma.2;
contract: specs/architecture/cc-event-mapping.md in the mu repo).

cc and mu sessions must sit on ONE event schema so analytics (markers, ML,
dashboards) read a single substrate. The prior MVP collapsed an entire cc
session into ONE `task_telemetry` summary + bare `tool_call` stubs — a contentless
record that told us nothing about the session. This emitter converts each cc
session into the rich, ordered `SessionEvent` stream the schema already supports:

  SessionCreated
  -> per turn: UserMessage | (AssistantMessageEvent + ToolCall*) | ToolResult* | Done
  -> TaskTelemetry   (session-summed; UNCHANGED from the MVP, so sink cost parity holds)

The installed `mu analytics compact` consumes these directly (it keys on
`task_telemetry` for the sink; the rich kinds feed engine.py). No mu rebuild.

Fidelity rules (operator requirement 2026-06-15 — preserve session information):
  - mu-consistent NORMALIZATIONS (not losses): cc `stop_sequence` -> EndTurn
    (matches anthropic.rs:678); cc `thinking{thinking,signature}` -> text-only
    (matches anthropic.rs:777 / accumulate.rs — mu drops the signature for its
    own sessions too).
  - NO SILENT DROPS: unrecognized assistant content-block types and skipped cc
    record types are COUNTED and printed; an unmapped block is preserved as a
    visible `[cc-unmapped-block:<type>]` text marker.
  - documented deferrals (no analytical value / no mu slot): non-token usage
    metadata (service_tier, server_tool_use, speed, ...), sub-agent `caller`.
"""

import collections
import glob
import json
import os
import re
import sys
from datetime import datetime

# Typed Anthropic parser (pyo3 wheel built from crates/providers/mu-anthropic-py
# -> lib/mu_anthropic_py.*.so). The SAME typed front door the proxy/drift job
# uses: is_valid_response_message() returns False when the wire shape no longer
# matches the typed model (Anthropic changed the spec) OR the message isn't
# Anthropic-shaped (openrouter cc account). We count those for an integrity
# signal; content/usage are read from the raw record (complete for our mapping).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    import mu_anthropic_py as _MA
except Exception:
    _MA = None

_PARSE = {"typed": 0, "fallback": 0}
# Visibility counters (printed at the end) — the anti-silent-drop guard.
_UNMAPPED_BLOCKS: "collections.Counter[str]" = collections.Counter()
_SKIPPED_TYPES: "collections.Counter[str]" = collections.Counter()

# cc record `type` values that are UI/metadata, not conversation turns. Counted
# (so a new one shows up loudly) but intentionally not mapped to a SessionEvent.
_NONCONVERSATION_TYPES = {
    "attachment",
    "last-prompt",
    "bridge-session",
    "system",
    "permission-mode",
    "ai-title",
    "mode",
    "file-history-snapshot",
    "queue-operation",
    "pr-link",
    "agent-name",
    "custom-title",
    "summary",
}


def iso_ms(ts: str) -> int:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _normalize_stop(s) -> str:
    """cc stop_reason -> mu StopReason (snake_case). stop_sequence folds to
    end_turn (mu's own anthropic provider does this, anthropic.rs:678)."""
    return {
        "tool_use": "tool_use",
        "end_turn": "end_turn",
        "max_tokens": "max_tokens",
        "stop_sequence": "end_turn",
    }.get(s, "end_turn")


def _normalize_usage(u: dict) -> dict:
    """cc usage -> mu Usage (only token fields; non-token metadata deferred).
    input/output always present (mu Usage requires them); rest are optional and
    omitted when absent/zero. `.get(k) or 0` coerces explicit JSON null."""
    out = {
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
    }
    cr = int(u.get("cache_read_input_tokens") or 0)
    if cr:
        out["cache_read_input_tokens"] = cr
    cw = int(u.get("cache_creation_input_tokens") or 0)
    if cw:
        out["cache_creation_input_tokens"] = cw
    cc = u.get("cache_creation") or {}
    c5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
    if c5:
        out["cache_creation_5m_input_tokens"] = c5
    c1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    if c1:
        out["cache_creation_1h_input_tokens"] = c1
    return out


def _has_tokens(usage: dict) -> bool:
    return bool(usage.get("input_tokens") or usage.get("output_tokens"))


def _stringify_result(content) -> str:
    """cc tool_result.content is a string OR a list of blocks. Flatten to the
    String mu's ToolResult holds, without dropping anything."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", "") or "")
            else:
                parts.append(json.dumps(b, separators=(",", ":")))
        return "\n".join(parts)
    return json.dumps(content, separators=(",", ":"))


def _normalize_assistant(m: dict) -> dict[str, object]:
    """Turn one cc assistant message into the pieces of an AssistantMessageEvent.

    Returns {model, stop_reason, usage, blocks, tool_uses, raw_usage}. `blocks`
    are mu-core ContentBlock JSON; `tool_uses` drive standalone ToolCall events;
    `raw_usage` feeds the UNCHANGED session-sum (cost parity)."""
    if _MA is not None:
        try:
            if _MA.is_valid_response_message(json.dumps(m)):
                _PARSE["typed"] += 1
            else:
                _PARSE["fallback"] += 1
        except Exception:
            _PARSE["fallback"] += 1
    else:
        _PARSE["fallback"] += 1

    model = m.get("model") or "unknown"
    stop = _normalize_stop(m.get("stop_reason"))
    raw_usage = m.get("usage") or {}
    usage = _normalize_usage(raw_usage)

    blocks = []
    tool_uses = []
    content = m.get("content")
    if isinstance(content, str):
        if content:
            blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                blocks.append({"type": "text", "text": b.get("text", "") or ""})
            elif bt == "thinking":
                # signature dropped (mu-consistent: thinking is display text only).
                blocks.append({"type": "thinking", "text": b.get("thinking", "") or ""})
            elif bt == "tool_use":
                tid = b.get("id", "") or ""
                name = b.get("name", "unknown") or "unknown"
                inp = b.get("input", {})
                if not isinstance(inp, (dict, list)):
                    inp = {} if inp is None else inp
                blocks.append({"type": "tool_call", "id": tid, "name": name, "arguments": inp})
                tool_uses.append({"id": tid, "name": name, "input": inp})
            else:
                # NO SILENT DROP: count + leave a visible marker.
                _UNMAPPED_BLOCKS[str(bt)] += 1
                blocks.append({"type": "text", "text": f"[cc-unmapped-block:{bt}]"})

    return {
        "model": model,
        "stop_reason": stop,
        "usage": usage,
        "blocks": blocks,
        "tool_uses": tool_uses,
        "raw_usage": raw_usage,
    }


# Harness-injected user-turn content is NOT operator language: skill bodies arrive
# as isMeta records (array-form text), and slash commands / command output / system
# notifications carry a structural tag at the start of the text. We still EMIT these
# as user_message (no information loss) but mark them `meta: true` so operator-
# language analytics (frustration/behavior scans, ML labels) can filter to operator
# speech only. Without this, "Base directory for this skill: …" and "<task-notification>"
# get counted as operator frustration.
_INJECTED_RX = re.compile(
    r"^\s*<(command-name|command-message|command-args|local-command-stdout|"
    r"local-command-caveat|system-reminder|task-notification|bash-input|bash-stdout|"
    r"bash-stderr)\b"
)


def _is_injected_text(text: str) -> bool:
    return bool(text) and bool(_INJECTED_RX.match(text))


def convert_session(path: str):
    """Return (session_id, [mu-core SessionEvent dicts]) or None if no assistant
    messages. Emits the full ordered stream; ids are monotonic from 1."""
    # Pass 1: collect ordered conversation records, deduping assistant messages
    # by id (streaming repeats the id with growing usage; last record wins —
    # mirrors mu_stats.sql cc_calls QUALIFY). `assistants` keeps raw usage for
    # the UNCHANGED session-sum so the sink's TaskTelemetry is byte-for-byte
    # what the MVP produced (cost parity).
    records = []  # ordered: {"type": user_text|tool_result|assistant, "ts":..., ...}
    asst_idx = {}  # mid -> index in records
    assistants = {}  # mid -> (raw_usage, model) for the session sum
    first_ts = last_ts = None

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            ts = iso_ms(o.get("timestamp", "")) if o.get("timestamp") else 0
            if ts:
                first_ts = ts if first_ts is None else min(first_ts, ts)
                last_ts = ts if last_ts is None else max(last_ts, ts)
            t = o.get("type")
            msg = o.get("message")
            if t == "assistant" and isinstance(msg, dict):
                mid = msg.get("id") or f"_n{len(asst_idx)}"
                norm = _normalize_assistant(msg)
                assistants[mid] = (norm.get("raw_usage") or {}, norm.get("model") or "unknown")
                rec = {"type": "assistant", "ts": ts, **norm}
                if mid in asst_idx:
                    records[asst_idx[mid]] = rec  # last wins, in place
                else:
                    asst_idx[mid] = len(records)
                    records.append(rec)
            elif t == "user" and isinstance(msg, dict):
                # isMeta marks harness-injected turns (e.g. skill bodies); the
                # per-text _is_injected_text catches tagged command/notification text.
                is_meta = bool(o.get("isMeta"))
                c = msg.get("content")
                if isinstance(c, str):
                    records.append(
                        {
                            "type": "user_text",
                            "ts": ts,
                            "content": c,
                            "meta": is_meta or _is_injected_text(c),
                        }
                    )
                elif isinstance(c, list):
                    # Emit per-block in document order: tool_result blocks become
                    # ToolResult; text blocks (user text in array form) become
                    # UserMessage — never dropped. Anything else is counted.
                    for b in c:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "tool_result":
                            records.append(
                                {
                                    "type": "tool_result",
                                    "ts": ts,
                                    "call_id": b.get("tool_use_id", "") or "",
                                    "content": _stringify_result(b.get("content")),
                                    "is_error": bool(b.get("is_error", False)),
                                }
                            )
                        elif bt == "text":
                            txt = b.get("text", "") or ""
                            if txt:
                                records.append(
                                    {
                                        "type": "user_text",
                                        "ts": ts,
                                        "content": txt,
                                        "meta": is_meta or _is_injected_text(txt),
                                    }
                                )
                        else:
                            _UNMAPPED_BLOCKS[f"user:{bt}"] += 1
            elif t:
                _SKIPPED_TYPES[t] += 1

    if not assistants:
        return None

    # Session sum (UNCHANGED from MVP — guarantees TaskTelemetry cost parity).
    pt = ct = cr = cw = cw5 = cw1 = 0
    mc = collections.Counter(
        mdl for _u, mdl in assistants.values() if mdl and mdl not in ("<synthetic>", "unknown")
    )
    model = mc.most_common(1)[0][0] if mc else "unknown"
    for usage, _mdl in assistants.values():
        if not isinstance(usage, dict):
            continue
        v = usage.get("input_tokens")
        pt += v if isinstance(v, int) else 0
        v = usage.get("output_tokens")
        ct += v if isinstance(v, int) else 0
        v = usage.get("cache_read_input_tokens")
        cr += v if isinstance(v, int) else 0
        v = usage.get("cache_creation_input_tokens")
        cw += v if isinstance(v, int) else 0
        cc = usage.get("cache_creation")
        if isinstance(cc, dict):
            v = cc.get("ephemeral_5m_input_tokens")
            cw5 += v if isinstance(v, int) else 0
            v = cc.get("ephemeral_1h_input_tokens")
            cw1 += v if isinstance(v, int) else 0

    sid = os.path.basename(path)
    if sid.endswith(".jsonl"):
        sid = sid[:-6]
    # Serving path drives cost_kind: openrouter account is pay-per-token (billed);
    # personal/work are Anthropic subscription (claude_code).
    provider = "openrouter" if "/.claude-openrouter/" in path else "claude_code"

    # Pass 2: assign monotonic ids, expand to the SessionEvent stream in order.
    events = []
    counter = [0]
    call_name = {}  # call_id -> tool name, for ToolResult actor attribution

    def emit(actor: dict, payload: dict, ts):
        counter[0] += 1
        events.append(
            {
                "id": counter[0],
                "session_id": sid,
                "timestamp_unix_ms": ts or 0,
                "actor": actor,
                "payload": payload,
            }
        )

    emit(
        {"kind": "system"},
        {"kind": "session_created", "provider_kind": provider, "model": model},
        first_ts,
    )

    ask_start_ts = first_ts
    asst_turns_in_ask = 0
    for rec in records:
        if rec["type"] == "user_text":
            payload = {"kind": "user_message", "content": rec["content"]}
            if rec.get("meta"):
                payload["meta"] = True  # harness-injected, not operator language
            emit({"kind": "user"}, payload, rec["ts"])
            ask_start_ts = rec["ts"] or ask_start_ts
            asst_turns_in_ask = 0
        elif rec["type"] == "tool_result":
            emit(
                {"kind": "tool", "name": call_name.get(rec["call_id"], "unknown")},
                {
                    "kind": "tool_result",
                    "call_id": rec["call_id"],
                    "content": rec["content"],
                    "is_error": rec["is_error"],
                },
                rec["ts"],
            )
        elif rec["type"] == "assistant":
            blocks = rec.get("blocks") or []
            stop_reason = rec.get("stop_reason")
            usage = rec.get("usage")
            tool_uses = rec.get("tool_uses") or []
            ts = rec.get("ts")
            message = {"content": blocks, "stop_reason": stop_reason}
            if isinstance(usage, dict) and _has_tokens(usage):
                message["usage"] = usage
            emit(
                {"kind": "agent"},
                {"kind": "assistant_message_event", "message": message},
                ts,
            )
            asst_turns_in_ask += 1
            # Standalone ToolCall events: the projector's tool_call_count reads
            # these (and tool analytics). Now with REAL call_id + arguments.
            if isinstance(tool_uses, list):
                for tu in tool_uses:
                    if not isinstance(tu, dict):
                        continue
                    cid = tu.get("id") or f"c{counter[0] + 1}"
                    call_name[cid] = tu.get("name", "unknown")
                    emit(
                        {"kind": "agent"},
                        {
                            "kind": "tool_call",
                            "call_id": cid,
                            "name": tu.get("name", "unknown"),
                            "arguments": tu.get("input", {}),
                        },
                        ts,
                    )
            # Done on a terminal turn (one per ask round-trip; tool_use turns continue).
            if stop_reason in ("end_turn", "max_tokens"):
                done = {
                    "kind": "done",
                    "stop_reason": stop_reason,
                    "turn_count": asst_turns_in_ask,
                }
                if isinstance(usage, dict) and _has_tokens(usage):
                    done["usage"] = usage
                if isinstance(ts, int) and isinstance(ask_start_ts, int) and ts and ask_start_ts:
                    done["elapsed_ms"] = max(0, ts - ask_start_ts)
                emit({"kind": "agent"}, done, ts)

    # Session-summed TaskTelemetry (UNCHANGED fields — sink contract / cost parity).
    tt = {
        "kind": "task_telemetry",
        "task_id": f"cc-{sid}",
        "session_id": sid,
        "provider_kind": provider,
        "model": model,
        "ended_at_unix_ms": last_ts or 0,
        "exit_reason": "done",
    }
    if first_ts:
        tt["started_at_unix_ms"] = first_ts
    if first_ts and last_ts:
        tt["wall_clock_ms"] = max(0, last_ts - first_ts)
    if pt:
        tt["prompt_tokens"] = pt
    if ct:
        tt["completion_tokens"] = ct
    if cr:
        tt["cache_read_tokens"] = cr
    if cw:
        tt["cache_write_tokens"] = cw
    if cw5:
        tt["cache_write_5m_tokens"] = cw5
    if cw1:
        tt["cache_write_1h_tokens"] = cw1
    emit({"kind": "system"}, tt, last_ts)

    return sid, events


def main():
    import tomllib

    here = os.path.dirname(os.path.abspath(__file__))
    if len(sys.argv) >= 3:  # ad-hoc: explicit <pattern> <out-dir>
        patterns, out_dir = [sys.argv[1]], sys.argv[2]
    else:  # default: ALL cc accounts from config
        cfg = tomllib.load(open(os.path.join(here, "config.toml"), "rb"))
        patterns = [os.path.join(r, "*", "*.jsonl") for r in cfg["paths"]["cc_log_roots"]]
        out_dir = cfg["paths"]["cc_events_out"]
    files = []
    for pat in patterns:
        files.extend(sorted(glob.glob(os.path.expanduser(pat))))
    daemon_dir = os.path.join(out_dir, "claude-code")
    os.makedirs(daemon_dir, exist_ok=True)
    n_sessions = n_events = 0
    kinds = collections.Counter()
    for f in files:
        try:
            res = convert_session(f)
        except PermissionError:
            continue
        if not res:
            continue
        sid, events = res
        with open(os.path.join(daemon_dir, f"{sid}.jsonl"), "w") as out:
            for ev in events:
                out.write(json.dumps(ev, separators=(",", ":")) + "\n")
                kinds[ev["payload"]["kind"]] += 1
        n_sessions += 1
        n_events += len(events)
    print(f"emitted {n_sessions} session(s), {n_events} events")
    print("  kinds: " + ", ".join(f"{k}={n}" for k, n in kinds.most_common()))
    note = "" if _MA else "  [mu_anthropic_py NOT importable — all fallback; build the wheel]"
    print(f"  parse: {_PARSE['typed']} typed (mu-anthropic), {_PARSE['fallback']} fallback{note}")
    if _UNMAPPED_BLOCKS:
        print(
            "  UNMAPPED blocks (preserved as markers, not dropped): "
            + ", ".join(f"{k}={n}" for k, n in _UNMAPPED_BLOCKS.most_common())
        )
    if _SKIPPED_TYPES:
        print(
            "  skipped non-conversation cc record types: "
            + ", ".join(f"{k}={n}" for k, n in _SKIPPED_TYPES.most_common(20))
        )


if __name__ == "__main__":
    main()
