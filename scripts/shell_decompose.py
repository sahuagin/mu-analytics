#!/usr/bin/env python3
"""Shell-command decomposition probe (throwaway-grade prototype, mu-gj0fd).

Replays every shell-bearing tool call in the session archive (cc Bash/Monitor,
mu bash/watch) and decomposes each command into individual statements. It
recurses into ssh / sh -c / python -c / heredocs / xargs / find -exec / python
subprocess, resolves VAR=literal, the working directory, heredoc-written
scripts and script files on disk, and reports:
  - how much of the traffic decomposes fully to literals (the guard's coverage)
  - what the unknowns are, by cause (the parser's honest residue)
  - how often a demo rule (a tree walk with no stay-on-one-filesystem flag)
    fires only after decomposition

Why it is kept: it is the baseline the Rust decomposer in mu (mu-gj0fd) must
match or beat on the same archive, and a quick way to ask "how often does X
appear / what would rule Y have caught" before that exists. DELETE IT once the
Rust archive-replay harness reproduces these numbers.

Run on the analytics host (it needs the archive and tree-sitter grammars):
    python3.11 scripts/shell_decompose.py <report.json> [extra-commands.jsonl]
Archive roots come from config.toml [paths] (cc_log_roots, mu_events_root).
The optional second argument is pre-extracted {"fleet","tool","cmd","cwd"}
lines, for transcripts the host cannot read directly.

Dependency: tree_sitter_languages (FreeBSD pkg py311-tree-sitter-languages). The
separately packaged grammars don't match the py311 binding's ABI.

The report includes up to 4 raw example commands per unknown cause. Those can
carry credentials, so write it somewhere private, never a shared /tmp.
"""

import collections
import glob
import json
import os
import re
import sys
import tomllib

from tree_sitter_languages import get_parser

P = {lang: get_parser(lang) for lang in ("bash", "python")}
MAX_DEPTH = 6

SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
PYTHONS = re.compile(r"^(python|python3|python3\.\d+|py)$")
OTHER_INTERP = {"node", "perl", "ruby", "lua", "php", "deno", "bun", "awk", "gawk", "jq"}
PREFIX = {
    "env",
    "sudo",
    "doas",
    "nice",
    "nohup",
    "time",
    "exec",
    "command",
    "builtin",
    "stdbuf",
    "ionice",
    "chrt",
    "setsid",
    "unbuffer",
}
OPTARG = {"timeout": 1}  # prefix commands that take N leading non-flag args


# ---- result accumulation -------------------------------------------------
class Result:
    def __init__(self, cwd=None):
        self.cwd = cwd  # working directory as far as it can be followed (None = unknown)
        self.cmds = []  # (depth, via, argv, literal_name, literal_all)
        self.unknown = collections.Counter()
        self.walks = []  # (depth, via, tool, confined, text)
        self.env = {}  # bash constant propagation: NAME -> literal (None = known-unknown)
        self.writes = {}  # path -> body, for files written by a heredoc earlier in the command
        self.resolved = collections.Counter()  # how script files were resolved

    def unk(self, why):
        self.unknown[why] += 1


# ---- bash ---------------------------------------------------------------
def text(n, src):
    return src[n.start_byte : n.end_byte].decode("utf-8", "replace")


DQ_ESC = re.compile(r'\\([$`"\\\n])')


def dq_unescape(s):
    """Backslash escapes that are live inside bash double quotes."""
    return DQ_ESC.sub(lambda m: "" if m.group(1) == "\n" else m.group(1), s)


SIMPLE_BRACE = re.compile(r"^\$\{(\w+)\}$")


def expansion_value(n, src, env):
    s = text(n, src)
    if n.type == "simple_expansion":
        name = s[1:]
    else:
        m = SIMPLE_BRACE.match(s)
        if not m:
            return None
        name = m.group(1)
    return env.get(name)  # missing or known-unknown -> None


def word_value(n, src, env):
    """Literal string value of a bash word node, or None if it can't be known."""
    t = n.type
    if t in ("word", "number"):
        s = text(n, src)
        return None if ("$" in s or "`" in s or "*" in s or "?" in s) else s.replace("\\", "")
    if t == "raw_string":
        return text(n, src)[1:-1]
    if t in ("simple_expansion", "expansion"):
        return expansion_value(n, src, env)
    if t == "string":
        # Rebuild the inside of "..." segment by segment: literal runs are
        # unescaped, $VAR / ${VAR} are substituted when known, anything else
        # ($(...), arithmetic, ${VAR:-x}) makes the whole word unknown.
        out, pos = [], n.start_byte + 1
        for c in n.named_children:
            if c.type == "string_content":
                continue
            out.append(dq_unescape(src[pos : c.start_byte].decode("utf-8", "replace")))
            v = (
                expansion_value(c, src, env)
                if c.type in ("simple_expansion", "expansion")
                else None
            )
            if v is None:
                return None
            out.append(v)
            pos = c.end_byte
        out.append(dq_unescape(src[pos : n.end_byte - 1].decode("utf-8", "replace")))
        return "".join(out)
    if t == "ansi_c_string":
        return text(n, src)[2:-1]
    if t == "concatenation":
        parts = [word_value(c, src, env) for c in n.children]
        return None if any(p is None for p in parts) else "".join(parts)
    return None


def record_assignment(n, src, env):
    name_node = n.child_by_field_name("name")
    val_node = n.child_by_field_name("value")
    if name_node is None:
        return
    name = text(name_node, src)
    env[name] = "" if val_node is None else word_value(val_node, src, env)


class child_process:
    """A nested shell (script file, sh -c, ssh remote) runs in its own process:
    its cd and variable assignments must not leak back into the caller."""

    def __init__(self, res, cwd="inherit"):
        self.res, self.cwd = res, cwd

    def __enter__(self):
        self.saved = (self.res.cwd, dict(self.res.env))
        if self.cwd != "inherit":
            self.res.cwd = self.cwd

    def __exit__(self, *exc):
        self.res.cwd, self.res.env = self.saved[0], self.saved[1]


def walk_bash(src_text, res, depth, via):
    if depth > MAX_DEPTH:
        res.unk("depth-limit")
        return
    src = src_text.encode()
    tree = P["bash"].parse(src)
    if tree.root_node.has_error:
        res.unk("bash-parse-error")
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "command":
            handle_bash_command(n, src, res, depth, via)
        elif n.type == "variable_assignment" and (n.parent is None or n.parent.type != "command"):
            record_assignment(n, src, res.env)  # standalone or `export X=...`
        elif n.type == "for_statement":
            v = n.child_by_field_name("variable")
            if v is not None:
                res.env[text(v, src)] = None  # loop variable: value not static
        stack.extend(reversed(n.children))


def heredoc_body_for(cmd_node, src):
    """Body of a heredoc attached to this command (redirected_statement)."""
    p = cmd_node.parent
    if p is not None and p.type == "redirected_statement":
        for c in p.children:
            if c.type == "heredoc_redirect":
                for cc in c.children:
                    if cc.type == "heredoc_body":
                        return text(cc, src)
    return None


def redirect_target(cmd_node, src, env):
    """Destination of a `> path` / `>> path` on this command, if literal."""
    for holder in (cmd_node, cmd_node.parent):
        if holder is None:
            continue
        for c in holder.children:
            if c.type == "file_redirect" and any(t.type in (">", ">>") for t in c.children):
                dest = c.child_by_field_name("destination")
                if dest is None and c.named_children:
                    dest = c.named_children[-1]
                if dest is not None:
                    return word_value(dest, src, env)
    return None


def handle_bash_command(n, src, res, depth, via):
    for c in n.children:  # prefix assignments: X=1 cmd
        if c.type == "variable_assignment":
            record_assignment(c, src, res.env)
    name_node = n.child_by_field_name("name")
    args = [
        c
        for c in n.children
        if c.type
        not in ("command_name", "variable_assignment", "file_redirect", "herestring_redirect")
        and c is not name_node
        and c.is_named
    ]
    name = (
        word_value(name_node.named_children[0], src, res.env)
        if name_node and name_node.named_children
        else None
    )
    argv_vals = [word_value(a, src, res.env) for a in args]
    if name is None:
        res.unk("dynamic-command-name")
        res.cmds.append((depth, via, ["?"], False, False))
        return
    argv = [name] + [v if v is not None else "\x00" for v in argv_vals]
    heredoc = heredoc_body_for(n, src)
    # remember files written from a heredoc (cat > f <<EOF / tee f <<EOF) so a
    # later `python f` / `bash f` in the same command can be parsed
    if heredoc is not None and os.path.basename(name) in ("cat", "tee"):
        dest = redirect_target(n, src, res.env)
        if dest is None and os.path.basename(name) == "tee":
            files = [a for a in argv[1:] if not a.startswith("-") and a != "\x00"]
            dest = files[0] if files else None
        if dest:
            res.writes[dest] = heredoc
            rp = resolve_path(dest, res)
            if rp:
                res.writes[rp] = heredoc
    dispatch(argv, all(v is not None for v in argv_vals), res, depth, via, heredoc=heredoc)


def resolve_path(path, res):
    p = os.path.expanduser(path)
    if os.path.isabs(p):
        return os.path.normpath(p)
    if res.cwd:
        return os.path.normpath(os.path.join(res.cwd, p))
    return None


def script_body(path, res):
    """Source of a script about to be run: written earlier in this command, or on disk."""
    if path in res.writes:
        res.resolved["same-command-heredoc"] += 1
        return res.writes[path]
    p = resolve_path(path, res)
    if p is not None and p in res.writes:
        res.resolved["same-command-heredoc"] += 1
        return res.writes[p]
    if p is None:
        return None
    if os.path.isabs(p) and os.path.isfile(p):
        try:
            with open(p, errors="replace") as fh:
                res.resolved["read-from-disk-now"] += 1
                return fh.read(2_000_000)
        except OSError:
            pass
    return None


def run_script(path, lang, res, depth, via):
    if path == "\x00":
        res.unk("runs-script-nonliteral-path")
        return
    body = script_body(path, res)
    if body is None:
        res.unk("runs-script-file-unresolved")
        return
    if lang is None:
        first = body.split("\n", 1)[0]
        lang = "python" if ("python" in first or path.endswith(".py")) else "bash"
    before = collections.Counter(res.unknown)
    with child_process(res):
        if lang == "python":
            walk_python(body, res, depth + 1, via + ["file:" + os.path.basename(path)])
        else:
            walk_bash(body, res, depth + 1, via + ["file:" + os.path.basename(path)])
    # unknowns found inside a script body are about the script, not the call:
    # a reviewed, hash-pinned script would not be descended into at all
    for k, v in (res.unknown - before).items():
        res.unknown[k] -= v
        if res.unknown[k] <= 0:
            del res.unknown[k]
        if not k.startswith("in-script:"):
            k = "in-script:" + k
        res.unknown[k] += v


# ---- argv dispatch (shared by bash and python subprocess) ---------------
def dispatch(argv, literal_all, res, depth, via, heredoc=None):
    name = os.path.basename(argv[0])
    rest = argv[1:]
    # prefix wrappers
    if name in PREFIX or name in OPTARG:
        i = 0
        skip = OPTARG.get(name, 0)
        while i < len(rest) and (rest[i].startswith("-") or "=" in rest[i] or skip > 0):
            if not rest[i].startswith("-") and "=" not in rest[i]:
                skip -= 1
            i += 1
        if i < len(rest):
            return dispatch(rest[i:], literal_all, res, depth, via + [name], heredoc)
        res.cmds.append((depth, via, argv, True, literal_all))
        return
    res.cmds.append((depth, via, argv, True, literal_all))
    check_walk(argv, res, depth, via)
    if name in ("cd", "pushd"):
        dest = [r for r in rest if not r.startswith("-")]
        if not dest:
            res.cwd = os.path.expanduser("~") if name == "cd" else res.cwd
        elif dest[0] == "\x00" or dest[0] == "-":
            res.cwd = None
        else:
            res.cwd = resolve_path(dest[0], res)
        return
    if name == "ssh":
        # skip options (-p 22, -o X, -i key ...), then host, then remote command
        i, takes = 0, set("bcDEeFIiJLlmOopQRSWw")
        while i < len(rest) and rest[i].startswith("-"):
            f = rest[i]
            i += 1
            if len(f) == 2 and f[1] in takes:
                i += 1
        i += 1  # host
        remote = rest[i:]
        if remote:
            if any(r == "\x00" for r in remote):
                res.unk("ssh-remote-nonliteral")
            else:
                with child_process(res, cwd=None):  # remote side: its own cwd and env
                    walk_bash(" ".join(remote), res, depth + 1, via + ["ssh"])
        elif heredoc:
            with child_process(res, cwd=None):
                walk_bash(heredoc, res, depth + 1, via + ["ssh<<"])
        return
    if name in SHELLS:
        if "-c" in rest:
            j = rest.index("-c") + 1
            if j < len(rest):
                if rest[j] == "\x00":
                    res.unk("sh-c-nonliteral")
                else:
                    with child_process(res):
                        walk_bash(rest[j], res, depth + 1, via + [name + " -c"])
        elif any(f.startswith("-") and not f.startswith("--") and "n" in f for f in rest):
            res.resolved["syntax-check-only"] += 1  # -n: parse only, nothing executes
        else:
            files = [r for r in rest if not r.startswith("-")]
            if heredoc and not files:
                walk_bash(heredoc, res, depth + 1, via + [name + "<<"])
            elif files:
                run_script(files[0], "bash", res, depth, via)
        return
    if name in ("source", "."):
        if rest:
            run_script(rest[0], "bash", res, depth, via)
        return
    if PYTHONS.match(name):
        files = [r for r in rest if not r.startswith("-")]
        if "-c" in rest:
            j = rest.index("-c") + 1
            if j < len(rest):
                if rest[j] == "\x00":
                    res.unk("python-c-nonliteral")
                else:
                    walk_python(rest[j], res, depth + 1, via + ["python -c"])
        elif "-m" in rest:
            pass  # module run: treat as an ordinary command
        elif heredoc is not None and (not files or files[0] == "-"):
            walk_python(heredoc, res, depth + 1, via + ["python<<"])
        elif files:
            run_script(files[0], "python", res, depth, via)
        return
    if "/" in argv[0] and (argv[0] in res.writes or argv[0].endswith((".sh", ".py"))):
        run_script(argv[0], None, res, depth, via)  # ./script.sh, $S/run.py
        return
    if name in OTHER_INTERP:
        if name in ("awk", "gawk", "jq"):
            return  # data languages: no exec surface worth descending for this demo
        if any(f in rest for f in ("-e", "-c", "--eval")) or heredoc:
            res.unk("interp-no-parser:" + name)
        return
    if name == "eval":
        res.unk("eval")
        return
    if name == "xargs":
        i = 0
        takes = {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"}
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in takes else 1
        if i < len(rest):
            dispatch(rest[i:], literal_all, res, depth + 1, via + ["xargs"])
        return
    if name == "find" and ("-exec" in rest or "-execdir" in rest or "-ok" in rest):
        for k, a in enumerate(rest):
            if a in ("-exec", "-execdir", "-ok"):
                sub = []
                for b in rest[k + 1 :]:
                    if b in (";", "\\;", "+"):
                        break
                    sub.append(b)
                if sub:
                    dispatch(sub, literal_all, res, depth + 1, via + ["find -exec"])


# ---- the demo rule ------------------------------------------------------
def check_walk(argv, res, depth, via):
    name = os.path.basename(argv[0])
    a = argv[1:]
    joined = " ".join(x for x in argv if x != "\x00")[:160]

    # confined = stays on one filesystem; a shallow depth bound (<=3) is
    # recorded as "bounded" so the report can separate it from a deep walk
    def bounded(flags):
        for k, x in enumerate(a):
            for f in flags:
                if x == f and k + 1 < len(a) and a[k + 1].isdigit():
                    return int(a[k + 1]) <= 3
                if x.startswith(f + "=") and x.split("=", 1)[1].isdigit():
                    return int(x.split("=", 1)[1]) <= 3
        return False

    if name == "find":
        conf = any(x in a for x in ("-x", "-xdev", "-mount"))
        res.walks.append((depth, via, "find", conf or bounded(("-maxdepth",)), joined, conf))
    elif name in ("fd", "fdfind"):
        conf = "--one-file-system" in a or "--mount" in a
        res.walks.append((depth, via, "fd", conf or bounded(("-d", "--max-depth")), joined, conf))
    elif name == "rg":
        conf = "--one-file-system" in a
        res.walks.append((depth, via, "rg", conf or bounded(("-d", "--max-depth")), joined, conf))
    elif name == "grep" and any(
        x in ("-r", "-R", "--recursive", "--dereference-recursive")
        or (x.startswith("-") and not x.startswith("--") and ("r" in x or "R" in x))
        for x in a
    ):
        res.walks.append((depth, via, "grep -r", False, joined, False))  # grep has no one-fs flag


# ---- python -------------------------------------------------------------
SUBPROC = {"run", "call", "check_call", "check_output", "Popen", "getoutput", "getstatusoutput"}


def py_literal(n, src):
    t = n.type
    if t == "string":
        if any(c.type == "interpolation" for c in n.named_children):
            return None
        s = text(n, src)
        try:
            import ast

            return ast.literal_eval(s)
        except Exception:
            return None
    if t in ("list", "tuple"):
        vals = [py_literal(c, src) for c in n.named_children]
        return None if any(v is None for v in vals) else vals
    if t == "concatenated_string":
        vals = [py_literal(c, src) for c in n.named_children]
        return None if any(v is None for v in vals) else "".join(vals)
    return None


def walk_python(code, res, depth, via):
    if depth > MAX_DEPTH:
        res.unk("depth-limit")
        return
    src = code.encode()
    tree = P["python"].parse(src)
    if tree.root_node.has_error:
        res.unk("python-parse-error")
    # single-pass constant propagation of NAME = literal
    consts = {}
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "assignment":
            lhs, r = n.child_by_field_name("left"), n.child_by_field_name("right")
            if lhs is not None and r is not None and lhs.type == "identifier":
                v = py_literal(r, src)
                if v is not None:
                    consts[text(lhs, src)] = v
        stack.extend(n.children)
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        stack.extend(n.children)
        if n.type != "call":
            continue
        fn = text(n.child_by_field_name("function"), src)
        argl = n.child_by_field_name("arguments")
        first = argl.named_children[0] if argl is not None and argl.named_children else None

        def lit(x):
            if x is None:
                return None
            if x.type == "identifier":
                return consts.get(text(x, src))
            return py_literal(x, src)

        tail = fn.split(".")[-1]
        if fn in ("eval", "exec", "compile", "__import__") or tail == "import_module":
            res.unk("python-eval/exec")
            continue
        if (
            fn in ("os.system", "os.popen")
            or (fn.startswith("subprocess.") and tail in SUBPROC)
            or (tail in SUBPROC and fn in ("run", "Popen", "check_output", "call"))
        ):
            v = lit(first)
            shell = "shell=True" in text(argl, src).replace(" ", "")
            if v is None:
                res.unk("python-subprocess-nonliteral")
            elif isinstance(v, list):
                if all(isinstance(x, str) for x in v) and v:
                    if shell:
                        walk_bash(" ".join(v), res, depth + 1, via + ["py-subprocess"])
                    else:
                        dispatch(v, True, res, depth + 1, via + ["py-subprocess"])
                else:
                    res.unk("python-subprocess-nonliteral")
            elif isinstance(v, str):
                walk_bash(v, res, depth + 1, via + ["py-" + tail])
            continue
        if (
            fn in ("os.walk", "os.fwalk")
            or tail in ("rglob",)
            or (tail in ("glob", "iglob") and "recursive=True" in text(argl, src).replace(" ", ""))
            or (tail == "glob" and first is not None and "**" in text(first, src))
        ):
            res.walks.append(
                (depth, via, "py-" + tail, False, fn, False)
            )  # no one-fs option exists


# ---- archive extraction ---------------------------------------------------
def cc_commands(root):
    unreadable = 0
    for dirpath, _dirs, files in os.walk(root, onerror=lambda e: None):
        if not os.access(dirpath, os.R_OK | os.X_OK):
            unreadable += 1
            continue
        for f in files:
            if not f.endswith(".jsonl"):
                continue
            p = os.path.join(dirpath, f)
            try:
                fh = open(p, errors="replace")
            except OSError:
                unreadable += 1
                continue
            with fh:
                for line in fh:
                    if '"tool_use"' not in line or (
                        '"Bash"' not in line and '"Monitor"' not in line
                    ):
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    c = (d.get("message") or {}).get("content")
                    if not isinstance(c, list):
                        continue
                    for b in c:
                        if b.get("type") == "tool_use" and b.get("name") in ("Bash", "Monitor"):
                            cmd = (b.get("input") or {}).get("command")
                            if isinstance(cmd, str):
                                yield ("cc", b["name"], cmd, d.get("cwd"))
    sys.stderr.write(f"cc unreadable dirs/files: {unreadable}\n")


def mu_commands(root):
    for p in glob.glob(os.path.join(root, "*", "events", "*", "*.jsonl")):
        try:
            fh = open(p, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"tool_call"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                pl = d.get("payload") or {}
                if pl.get("kind") == "tool_call" and pl.get("name") in ("bash", "watch"):
                    cmd = (pl.get("arguments") or {}).get("command")
                    if isinstance(cmd, str):
                        yield ("mu", pl["name"], cmd, None)  # mu tool calls carry no cwd


# ---- main ---------------------------------------------------------------
NAIVE = re.compile(r"^\s*(sudo\s+)?(find|fd|fdfind|rg|grep)\b")


def archive_roots():
    """Archive roots from config.toml [paths]; MU_ANALYTICS_CONFIG overrides the file.

    Fails loudly: a root that does not exist would otherwise yield an empty,
    plausible-looking report (the committed example config has placeholders).
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg_path = os.environ.get("MU_ANALYTICS_CONFIG") or os.path.join(here, "config.toml")
    if not os.path.exists(cfg_path):
        sys.exit(
            f"shell_decompose: no config at {cfg_path}. Copy config.example.toml to "
            "config.toml and set [paths] cc_log_roots / mu_events_root, or point "
            "MU_ANALYTICS_CONFIG at an existing mu-analytics config.toml."
        )
    with open(cfg_path, "rb") as fh:
        paths = tomllib.load(fh)["paths"]
    cc_roots, mu_root = paths["cc_log_roots"], paths["mu_events_root"]
    missing = [r for r in [*cc_roots, mu_root] if not os.path.isdir(r)]
    if missing:
        sys.exit(
            f"shell_decompose: archive root(s) not found: {', '.join(missing)} "
            f"(from {cfg_path} [paths] cc_log_roots / mu_events_root)."
        )
    return cc_roots, mu_root


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out = sys.argv[1]
    cc_roots, mu_root = archive_roots()
    stats = collections.Counter()
    reasons = collections.Counter()
    reason_ex = collections.defaultdict(list)
    via_ctr = collections.Counter()
    walk_tools = collections.Counter()
    nested_walk_ex = []
    seen = set()
    extra = []
    if (
        len(sys.argv) > 2
    ):  # pre-extracted {"fleet","tool","cmd"} lines (transcripts this host can't read)
        with open(sys.argv[2]) as fh:
            extra = [(r["fleet"], r["tool"], r["cmd"], r.get("cwd")) for r in map(json.loads, fh)]
    calls = [c for root in cc_roots for c in cc_commands(root)]
    calls += list(mu_commands(mu_root)) + extra
    if not calls:
        sys.exit(
            f"shell_decompose: found no shell-bearing tool calls under {cc_roots} and "
            f"{mu_root}; check the roots, and that this user can read the transcripts."
        )
    for fleet, tool, cmd, cwd in calls:
        stats["calls_with_cwd"] += cwd is not None
        stats[f"calls:{fleet}:{tool}"] += 1
        stats["calls"] += 1
        key = cmd.strip()  # cmd-only, to stay comparable with v1/v2; first cwd seen wins
        if key in seen:
            stats["duplicate"] += 1
            continue
        seen.add(key)
        stats["unique"] += 1
        res = Result(cwd)
        try:
            walk_bash(cmd, res, 0, [])
        except RecursionError:
            res.unk("recursion")
        for k, v in res.resolved.items():
            stats["script_resolved:" + k] += v
        nested = any(d > 0 for d, *_ in res.cmds)
        compound = len(res.cmds) > 1
        stats["compound"] += compound
        stats["nested"] += nested
        stats[f"max_depth_{max([d for d, *_ in res.cmds] or [0])}"] += 1
        for _d, via, _argv, ln, la in res.cmds:
            stats["statements"] += 1
            stats["stmt_name_literal"] += ln
            stats["stmt_all_literal"] += ln and la
            if via:
                via_ctr[" > ".join(via)] += 1
        if not [k for k in res.unknown if not k.startswith("in-script:")]:
            stats["unique_resolved_at_call_level"] += 1
        if res.unknown:
            stats["unique_with_unknown"] += 1
            for r in res.unknown:
                reasons[r] += 1
                if len(reason_ex[r]) < 4:
                    reason_ex[r].append(cmd[:200])
        else:
            stats["unique_fully_resolved"] += 1
        naive_hit = bool(NAIVE.match(cmd))
        unconfined = [w for w in res.walks if not w[3]]
        if res.walks:
            stats["unique_with_walk"] += 1
        for w in res.walks:
            stats["walk_statements"] += 1
            stats["walk_fs_confined"] += w[5]
            stats["walk_bounded_only"] += w[3] and not w[5]
            stats["walk_unconfined_deep"] += not w[3]
        if unconfined:
            stats["rule_hit_decomposed"] += 1
            stats["rule_hit_naive"] += naive_hit
            if not naive_hit:
                stats["rule_hit_only_after_decomposition"] += 1
            for w in unconfined:
                walk_tools[(w[2], f"depth{w[0]}", " > ".join(w[1]) or "top")] += 1
                if w[0] > 0 and len(nested_walk_ex) < 12:
                    nested_walk_ex.append(" > ".join(w[1]) + " :: " + w[4])
    report = {
        "stats": dict(stats),
        "unknown_reasons": reasons.most_common(),
        "unknown_examples": {k: v for k, v in reason_ex.items()},
        "nesting_paths": via_ctr.most_common(25),
        "unconfined_walks": [(list(k), v) for k, v in walk_tools.most_common(30)],
        "nested_walk_examples": nested_walk_ex,
    }
    with open(out, "w") as fh:
        json.dump(report, fh, indent=1)
    print(json.dumps({k: report[k] for k in ("stats", "unknown_reasons")}, indent=1))


if __name__ == "__main__":
    sys.setrecursionlimit(10000)
    main()
