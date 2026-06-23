#!/usr/bin/env python3
"""Deterministic violation classifiers — base rates per fleet (standalone, raw-jsonl).

Each classifier is a harvested hook predicate (see docs/directive-adherence.md)
expressed over a session's ORDERED tool stream — the "hook as classifier" idea.
Age-independent: these are anti-patterns regardless of when any directive was
added, so they're meaningful on the local subset without the directive-timeline
tiering (which is bound to the analytics host for the none->doc test).

Benchmarks are EXCLUDED (path contains 'bench') — they're a flat control
population, not real work.

Graduates into mu audit auditors / features.py over the `ev` view for the analytics host run.
"""

import glob
import json
import os
import re

READ = {"read"}
EDIT = {"edit", "write", "multiedit", "notebookedit", "applypatch", "apply_patch"}
BASH = {"bash", "shell", "run"}

RX_DANGER = re.compile(
    r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r|\bdd\s+.*\bof=|\bmkfs\b|:\(\)\s*\{", re.I
)
RX_FORCE = re.compile(
    r"\b(git|jj)\b.*(push\s+.*(--force|-f)\b|reset\s+--hard|push\s+--force|branch\s+-D)", re.I
)
# Heredoc / shell-write anti-patterns (operator insight 2026-06-22): heredocs are
# unauditable in the approval dialog (only a slice shows) and shell file-writes
# bypass the Write tool's Read-before-overwrite guard (overwrite-blindness).
RX_HEREDOC = re.compile(r"<<-?\s*['\"\\]?[A-Za-z_]")  # a heredoc operator
RX_CODE_HEREDOC = re.compile(
    r"\b(python3?|node|deno|ruby|perl|php|jq|psql|sqlite3|Rscript)\b[^\n]{0,60}<<"
)
# A real file-write redirect: a `>`/`>>` NOT preceded by a fd digit (`2>`) or `&`,
# target is a path or a filename.ext, excluding /dev/*. Plus `tee <file>`. The loose
# v1 (echo|cat anywhere + a later `>`) was FP-heavy on `… 2>/dev/null` (FP-audit
# 2026-06-22) — heredoc is the clean signal; this is the noisier secondary one.
RX_SHELL_WRITE = re.compile(
    r"(?<![0-9&])>{1,2}\s*(?!/dev/)(?!/tmp)(?!/var/tmp)(?!\$\{?TMP)(?!&)([~./][^\s&|;>]*|[A-Za-z][\w.-]*\.[A-Za-z][\w.]*)|\btee\s+(?!-)[^\s&|;>]",
    re.I,
)


def _argval(args, *keys):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return args  # raw string (e.g. a bash command stored as a string)
    if isinstance(args, dict):
        for k in keys:
            if args.get(k):
                return args[k]
    return ""


def cc_tools(f):
    """Ordered (name_lower, args) from cc assistant tool_use blocks."""
    out = []
    for line in open(f, errors="ignore"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        msg = o.get("message") if isinstance(o.get("message"), dict) else None
        content = msg.get("content") if msg else None
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    out.append((str(c.get("name", "")).lower(), c.get("input", {})))
    return out


def mu_tools(f):
    """Ordered (name_lower, args) from mu tool_call events."""
    out = []
    for line in open(f, errors="ignore"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        p = o.get("payload") if isinstance(o.get("payload"), dict) else None
        if p and p.get("kind") == "tool_call":
            out.append((str(p.get("name", "")).lower(), p.get("arguments", {})))
    return out


def violations(tools):
    """Return the set of predicate names this session violates."""
    v = set()
    read_files = set()
    edit_counts = {}
    for name, args in tools:
        f = _argval(args, "file_path", "path", "notebook_path")
        cmd = _argval(args, "command", "cmd")
        if not isinstance(cmd, str):
            cmd = ""
        if name in READ and f:
            read_files.add(f)
        if name in EDIT:
            if f and f not in read_files:
                v.add("edit_before_read")
            if f:
                edit_counts[f] = edit_counts.get(f, 0) + 1
                if edit_counts[f] >= 5:
                    v.add("edit_loop")
        if name in BASH and cmd:
            if RX_DANGER.search(cmd):
                v.add("dangerous_bash")
            if RX_FORCE.search(cmd):
                v.add("force_push")
            if RX_HEREDOC.search(cmd):
                v.add("heredoc")
            if RX_CODE_HEREDOC.search(cmd):
                v.add("code_in_heredoc")
            if RX_SHELL_WRITE.search(cmd):
                v.add("shell_file_write")
            if len(cmd) > 1200:
                v.add("large_bash")
    return v


def run(name, files, parse):
    PREDS = [
        # pure tool-stream (trust): heredoc/shell-write + bash hazards
        "heredoc",
        "code_in_heredoc",
        "shell_file_write",
        "large_bash",
        "dangerous_bash",
        "force_push",
        "edit_loop",
        # context-state (noisy from logs; runtime-only — see docs)
        "edit_before_read",
    ]
    n_tool = 0
    counts = {p: 0 for p in PREDS}
    for f in files:
        tools = parse(f)
        if not tools:
            continue
        n_tool += 1
        for p in violations(tools):
            counts[p] += 1
    print(f"\n[{name}] sessions with tools: {n_tool}")
    if n_tool:
        for p in PREDS:
            print(f"  {p:18} {counts[p]:>4}  ({100 * counts[p] // n_tool:>3}%)")


def main():
    cc = [
        f for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")) if "bench" not in f
    ]
    mu = glob.glob(os.path.expanduser("~/.local/share/mu/events/*/session-*.jsonl"))
    run("cc-real", cc, cc_tools)
    run("mu", mu, mu_tools)


if __name__ == "__main__":
    main()
