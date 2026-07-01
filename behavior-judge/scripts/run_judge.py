#!/usr/bin/env python3
"""Run the behavior-judge on one (transcript, class).

Resolves the model through the operator's role system — `agent-role judge` gives a
ranked (provider, model) ladder — and runs the resolved target through the SHARED
dispatcher (`agent_dispatch`, sourced from agent-dispatch.sh: the one thing
everything should use). It routes claude-vs-mu ToS-cleanly, holds the cooperative
ollama lease, and stays hermetic; `agent-role` itself demotes off a busy ollama box,
so a contended box routes you down to codex/opus. Nothing about host/model/sampling
is hardcoded here: it lives in ~/.config/mu (agent_roles.toml, models.toml) and the
shared dispatcher. A `--host`/`--model` escape hatch keeps a direct ollama call for a
standalone/publishable checkout with no agent-role.

System prompt = judge/behavior-judge-system-prompt.txt with {CLASS_RUBRIC} filled
from judge/rubric.md for the given class, passed via --append-system-prompt.

Usage: run_judge.py --transcript <rendered.txt> --cls <class-id> [--role R] [--host H --model M]
Prints the verdict to stdout; the chosen provider/model + timing to stderr.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
JUDGE = os.path.join(HERE, "..", "judge")


def class_rubric(cls):
    text = open(os.path.join(JUDGE, "rubric.md")).read()
    for blk in text.split("\n## "):
        if blk.strip().startswith(cls):
            return "## " + blk.strip()
    raise SystemExit(f"class '{cls}' not found in rubric.md")


ROLE_DEFAULT = "judge"


def coerce_json(text):
    """The verdict object out of a model's reply, or None. Dispatched models wrap the
    JSON in ```json fences and may emit <think> reasoning first (qwen3 et al.), so the
    raw reply isn't parseable as-is. Extract the outermost {...} and validate it — that
    survives fences, thinking, and prose without caring which the model used."""
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j <= i:
        return None
    try:
        return json.loads(text[i : j + 1])
    except json.JSONDecodeError:
        return None


def role_ladder(role):
    """Ranked `(provider, model)` targets for ROLE, resolved from the operator's
    config via `agent-role` — the alternative to hardcoding a model id. Returns []
    if agent-role is absent (a publishable/standalone checkout), so the caller can
    fall back to a direct call."""
    try:
        out = subprocess.run(["agent-role", role], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    ladder = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            ladder.append((parts[0], parts[1]))
    return ladder


def _dispatch_lib():
    """Path to the canonical dispatcher (mu/scripts/lib/agent-dispatch.sh), preferring
    the ~/.local/bin symlink so this isn't coupled to the mu repo's location."""
    for p in (
        "~/.local/bin/agent-dispatch.sh",
        "~/src/public_github/mu/scripts/lib/agent-dispatch.sh",
    ):
        full = os.path.expanduser(p)
        if os.path.exists(full):
            return full
    return os.path.expanduser("~/.local/bin/agent-dispatch.sh")


def dispatch(provider, model, sys_file, transcript_path, timeout):
    """Run one resolved target through the SHARED dispatcher — `agent_dispatch`, sourced
    from agent-dispatch.sh, the one thing everything should use. It routes claude-vs-mu
    ToS-cleanly, holds the cooperative ollama lease, and stays hermetic; `agent-role`'s
    demote-when-held already steers resolution off a busy box. The judge needs no tools
    (TOOLS=''); the class rubric is the system prompt. Returns (verdict_text, ok)."""
    script = '. "$AGENT_DISPATCH_LIB" && agent_dispatch "$1" "$2" "$3"'
    env = {
        **os.environ,
        "AGENT_DISPATCH_LIB": _dispatch_lib(),
        "SYSPROMPT": sys_file,
        "TOOLS": "",  # pure read-transcript -> verdict; no read/grep/bash tools
        "TIMEOUT": str(timeout),
    }
    r = subprocess.run(
        ["sh", "-c", script, "sh", provider, model, transcript_path],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout + 60,
    )
    text = (r.stdout or "").strip()
    return text, bool(text)


def direct_ollama(host, model, system, transcript_path, timeout):
    """Standalone/publishable fallback: a direct ollama /api/chat call (the original
    behaviour) for a checkout with no agent-role. Model AS-LOADED — no sampling or
    num_ctx overrides (temperature 0 degenerates qwen3; changing num_ctx reloads)."""
    transcript = open(transcript_path, errors="ignore").read()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Rendered transcript follows.\n\n" + transcript},
        ],
        "stream": False,
        "format": "json",
    }
    req = urllib.request.Request(
        "http://" + host + "/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    out = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    return out.get("message", {}).get("content", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--cls", required=True)
    ap.add_argument(
        "--role", default=ROLE_DEFAULT, help="agent-role to resolve the model (default: judge)"
    )
    ap.add_argument("--timeout", type=int, default=900)
    # Standalone escape hatch: an explicit --host forces a direct ollama call,
    # bypassing role resolution. NOT used in the operator's deployment.
    ap.add_argument("--host", default=None, help="direct ollama host:port, bypassing agent-role")
    ap.add_argument("--model", default=None, help="direct ollama model (used with --host)")
    ap.add_argument(
        "--skip-ollama",
        action="store_true",
        help="drop ollama targets from the ladder — route to the concurrent subscription "
        "APIs (gpt-5.5/opus) so parallel workers actually overlap. NOT the calibrated qwen; "
        "for the historical backfill. The runner stamps which model judged each verdict.",
    )
    args = ap.parse_args()

    sys_t = open(os.path.join(JUDGE, "behavior-judge-system-prompt.txt")).read()
    system = sys_t.replace("{CLASS_RUBRIC}", class_rubric(args.cls))

    if args.host:  # direct/standalone mode
        print(
            direct_ollama(
                args.host,
                args.model or "qwen3.6:35b-a3b-q8_0",
                system,
                args.transcript,
                args.timeout,
            )
        )
        return

    ladder = role_ladder(args.role)
    if args.skip_ollama:
        ladder = [(p, m) for p, m in ladder if not p.startswith("ollama")]
    if not ladder:
        why = (
            "has no non-ollama targets (--skip-ollama removed them all)"
            if args.skip_ollama
            else "did not resolve (is agent-role on PATH?)"
        )
        sys.exit(
            f"judge: role '{args.role}' {why}. Pass --host/--model for a direct standalone call."
        )

    # The class system-prompt goes to a temp file for --append-system-prompt.
    with tempfile.NamedTemporaryFile("w", suffix=".sysprompt", delete=False) as sf:
        sf.write(system)
        sys_file = sf.name
    try:
        for provider, model in ladder:
            t0 = time.time()
            text, ok = dispatch(provider, model, sys_file, args.transcript, args.timeout)
            verdict = coerce_json(text) if ok else None
            if verdict is not None:
                # Stamp WHICH target produced this verdict — only the rank-0 ollama model is
                # rubric-validated; a deranked/busy box routes to fallbacks whose verdicts the
                # consumer must be able to tell apart. Survives in the verdict's own JSON.
                verdict["judge_model"] = f"{provider}/{model}"
                sys.stderr.write(f"[{args.cls}] {provider}/{model} {time.time() - t0:.0f}s\n")
                print(json.dumps(verdict))  # clean JSON to stdout — the parseable contract
                return
            why = "unavailable (busy/error)" if not ok else "returned no parseable JSON"
            sys.stderr.write(f"[{args.cls}] {provider}/{model} {why} -> next rank\n")
        sys.exit(f"judge: no target in role '{args.role}' produced a verdict")
    finally:
        os.unlink(sys_file)


if __name__ == "__main__":
    main()
