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
import re
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
    """The verdict object out of a model's reply, or None. Dispatched models wrap the JSON
    in ```json fences and emit <think>/[thinking] reasoning first (qwen3 et al.), so the raw
    reply isn't parseable as-is. Tolerate the mess instead of aborting on it: drop the
    thinking, prefer a fenced json block, else fall back to the outermost {...}. A reply
    that doesn't `json.loads` cleanly is almost always a good verdict wearing a fence or a
    reasoning trace; treating it as a hard failure needlessly routes the ladder down to the
    next (non-calibrated) model. (Folded from PR #66, which this supersedes.)"""
    s = re.sub(r"(?is)<think>.*?</think>", "", text)
    s = re.sub(r"(?im)^\s*\[thinking\].*?$", "", s)
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", s, re.S)
    if m:
        s = m.group(1)
    else:
        i, j = s.find("{"), s.rfind("}")
        if i == -1 or j <= i:
            return None
        s = s[i : j + 1]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Leading-zero integers ("turn":009 — qwen3.8 emits them) are the one
        # observed strict-JSON violation. Repair only after a failed parse so
        # a valid verdict's quote text can never be rewritten.
        try:
            return json.loads(re.sub(r'("\w+"\s*:\s*)0+(\d)', r"\1\2", s))
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
    # HARD billing guard: strip every ANTHROPIC* var so a key present in the ambient
    # environment (shell export, cron env) can never route a judge call to the metered
    # API — claude targets resolve to the OAuth subscription or fail. The judge's
    # ladder wants only subscription/local targets; a metered fallback is never correct.
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC")},
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


def verify_evidence(verdict, transcript_path):
    """Stamp n_evidence_verified: how many evidence quotes literally appear in the
    transcript (whitespace-normalized substring). The verbatim-evidence requirement is
    the rubric's anchor, and fabricated quotes are a known failure mode (universal in
    the local-model routing bench) — consumers can discount a verdict whose quotes
    don't check out instead of trusting the count of claimed evidence."""

    def norm(s):
        return " ".join(str(s).split())

    hay = norm(open(transcript_path, errors="ignore").read())
    ev = verdict.get("evidence") or []
    verdict["n_evidence_verified"] = sum(
        1 for e in ev if isinstance(e, dict) and e.get("quote") and norm(e["quote"]) in hay
    )
    return verdict


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
        model = args.model or "qwen3.6:35b-a3b-q8_0"
        text = direct_ollama(args.host, model, system, args.transcript, args.timeout)
        verdict = coerce_json(text)
        if verdict is None:
            # keep the raw reply visible for debugging, but fail loudly — an
            # unparseable verdict must not read as a clean run.
            print(text)
            sys.exit(f"judge: ollama/{model} returned no parseable JSON verdict")
        verdict["judge_model"] = f"ollama/{model}"
        print(json.dumps(verify_evidence(verdict, args.transcript)))
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
                verify_evidence(verdict, args.transcript)
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
