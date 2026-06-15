#!/usr/bin/env python3
"""Emit current-mu-core TaskTelemetry (+ ToolCall) events from cc session logs.

MVP spine: one cc session -> one `task_telemetry` event (exit_reason=done,
session-summed deduped tokens) preceded by its `tool_call` events (so the
mu-042 projector's tool_call_count is honest). The installed `mu analytics
compact` consumes these directly. No mu rebuild, no mu-repo edits.

Calibration knobs intentionally simple for the MVP (refine by looking):
  - one cc session == one task
  - exit_reason == "done" for every cc session
  - provider_kind == "claude_code" so cc is visibly distinct from mu's anthropic
"""

import glob
import json
import os
import sys
from datetime import datetime

# Typed Anthropic parser (pyo3 wheel built from crates/providers/mu-anthropic-py
# -> lib/mu_anthropic_py.*.so). The SAME typed front door the proxy/drift job
# uses: is_valid_response_message() returns False when the wire shape no longer
# matches the typed model (Anthropic changed the spec) OR the message isn't
# Anthropic-shaped (openrouter cc account). We count those and fall back.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    import mu_anthropic_py as _MA
except Exception:
    _MA = None

_PARSE = {"typed": 0, "fallback": 0}


def iso_ms(ts: str) -> int:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def extract_message(message: dict):
    """(usage, model, [(tool_name, tool_id)]) for one cc assistant message.
    Typed via mu-anthropic when valid; hand-rolled fallback otherwise (counted)."""
    if _MA is not None:
        s = json.dumps(message)
        if _MA.is_valid_response_message(s):
            norm = json.loads(_MA.parse_response_message(s))
            tools = [
                (b.get("name", "unknown"), b.get("id", ""))
                for b in (norm.get("content") or [])
                if b.get("type") == "tool_use"
            ]
            _PARSE["typed"] += 1
            return (norm.get("usage") or {}, norm.get("model") or "unknown", tools)
    _PARSE["fallback"] += 1
    tools = [
        (b.get("name", "unknown"), b.get("id", ""))
        for b in (message.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    return (message.get("usage") or {}, message.get("model") or "unknown", tools)


def convert_session(path: str):
    """Return (session_id, [mu-core SessionEvent dicts]) or None if no assistant msgs."""
    # Dedup assistant usage by message id (streaming repeats the id with growing
    # usage; last record wins — mirrors mu_stats.sql cc_calls QUALIFY).
    assistants = {}  # msg_id -> (usage, model)
    tool_calls = []  # (name, call_id)
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
            if o.get("type") == "assistant" and isinstance(o.get("message"), dict):
                m = o["message"]
                mid = m.get("id") or f"_n{len(assistants)}"
                usage, model, tools = extract_message(m)
                assistants[mid] = (usage, model)
                tool_calls.extend(tools)

    if not assistants:
        return None

    pt = ct = cr = cw = cw5 = cw1 = 0
    # Dominant *real* model: cc tags system-injected turns with "<synthetic>";
    # pick the most frequent model that isn't synthetic/unknown.
    import collections

    mc = collections.Counter(
        mdl for _u, mdl in assistants.values() if mdl and mdl not in ("<synthetic>", "unknown")
    )
    model = mc.most_common(1)[0][0] if mc else "unknown"
    for usage, _mdl in assistants.values():
        # `.get(k, 0)` returns None when the key exists but is null (work/
        # openrouter accounts do this) — coerce with `or 0`.
        pt += usage.get("input_tokens") or 0
        ct += usage.get("output_tokens") or 0
        cr += usage.get("cache_read_input_tokens") or 0
        cw += usage.get("cache_creation_input_tokens") or 0
        cc = usage.get("cache_creation") or {}
        cw5 += cc.get("ephemeral_5m_input_tokens") or 0
        cw1 += cc.get("ephemeral_1h_input_tokens") or 0

    sid = os.path.basename(path)
    if sid.endswith(".jsonl"):
        sid = sid[:-6]
    # Serving path drives cost_kind: the openrouter account is pay-per-token
    # (billed); personal/work are Anthropic subscription (claude_code).
    provider = "openrouter" if "/.claude-openrouter/" in path else "claude_code"

    events = []
    eid = 1
    for name, cid in tool_calls:
        events.append(
            {
                "id": eid,
                "session_id": sid,
                "timestamp_unix_ms": first_ts or 0,
                "actor": {"kind": "agent"},
                "payload": {
                    "kind": "tool_call",
                    "call_id": cid or f"c{eid}",
                    "name": name,
                    "arguments": {},
                },
            }
        )
        eid += 1

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

    events.append(
        {
            "id": eid,
            "session_id": sid,
            "timestamp_unix_ms": last_ts or 0,
            "actor": {"kind": "system"},
            "payload": tt,
        }
    )
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
    n_sessions = n_tasks = n_tools = 0
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
        n_sessions += 1
        n_tasks += 1
        n_tools += sum(1 for e in events if e["payload"]["kind"] == "tool_call")
    print(f"emitted {n_sessions} session(s), {n_tasks} task_telemetry, {n_tools} tool_call events")
    note = "" if _MA else "  [mu_anthropic_py NOT importable — all fallback; build the wheel]"
    print(f"  parse: {_PARSE['typed']} typed (mu-anthropic), {_PARSE['fallback']} fallback{note}")


if __name__ == "__main__":
    main()
