#!/usr/bin/env python3
"""Incident-report timeline source — parse the operator's notes directory of
incident reports / postmortems into dated events for the Overview cost+degradation
timeline.

The operator writes reports into one or more notes dirs ([incidents].dirs, or the
single-path [incidents].dir; default ~/.claude/notes) — on the dashboard host that
is the consolidated cross-machine archive plus the host's own local notes, unioned
by filename. Each file becomes {date, title, polarity, kind, slug, session_refs,
file}. Polarity is by
filename prefix and is config-overridable ([incidents].polarity):
  incident-*  -> issue      (something went wrong; a postmortem)
  checkpoint-* -> positive  (landed work / validated milestone)
Unmapped prefixes (design notes, scans) are skipped so the timeline stays
report-focused.

Session refs follow the incident-session-provenance contract (agent memory
dc6e7e9e): fleet-prefixed cc:<uuid> / mu:<daemon>/<session>, the SAME key as
operator marks and the event log. We normalize the mu slash-form to the colon-form
the ev view / marks use (mu:<daemon>:<session>) so an incident lines up with its
marked session on the timeline.

Read-only, stdlib only. Run:  ./run incidents.py   (prints the parsed timeline)
"""

import glob
import os
import re
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = os.path.join(HERE, "config.toml")
if not os.path.exists(_CFG):
    _CFG = os.path.join(HERE, "config.example.toml")
_cfg = tomllib.load(open(_CFG, "rb"))
_icfg = _cfg.get("incidents", {})

# Notes dirs: [incidents].dirs (list) wins; else [incidents].dir (str); default
# ~/.claude/notes. Multiple dirs let the dashboard host read the consolidated
# cross-machine archive AND its own local notes in one pass (union by filename).
_raw_dirs = _icfg.get("dirs") or [_icfg.get("dir", "~/.claude/notes")]
DIRS = [os.path.expanduser(d) for d in _raw_dirs]
# filename-prefix -> timeline polarity; [incidents].polarity overrides/extends.
POLARITY = {"incident": "issue", "checkpoint": "positive"}
POLARITY.update(_icfg.get("polarity", {}))

_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# fleet-prefixed session refs (incident-session-provenance-contract):
#   cc:<uuid 8-4-4-4-12>  |  mu:<daemon>[:/]<session>
_SREF = re.compile(
    r"\bcc:[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"
    r"|\bmu:[A-Za-z0-9_]+[:/][A-Za-z0-9_.-]+"
)


def _title(path):
    """First markdown H1, else the filename."""
    try:
        with open(path, errors="ignore") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("# "):
                    return s[2:].strip()
    except OSError:
        pass
    return os.path.basename(path)


def _refs(body):
    """Unique session_refs in file order, mu slash-form normalized to colon-form."""
    seen, out = set(), []
    for m in _SREF.findall(body):
        ref = m.replace("mu:", "mu:", 1)
        if ref.startswith("mu:"):
            ref = "mu:" + ref[3:].replace("/", ":", 1)
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def load(dirs=None):
    """Parse incident reports from one or more notes dirs ->
    [{date,title,polarity,kind,slug,session_refs,file}], date-sorted. Dirs are
    unioned by filename (first dir wins on a basename collision), so the dashboard
    host can read the consolidated cross-machine archive AND its own local notes in
    one pass. Accepts a single path (str) or a list; defaults to the configured
    DIRS. Empty when no dir exists (CI / a host without the notes mount)."""
    if dirs is None:
        dirs = DIRS
    elif isinstance(dirs, str):
        dirs = [dirs]
    seen = set()
    out = []
    for d in dirs:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for path in sorted(glob.glob(os.path.join(d, "*.md"))):
            fname = os.path.basename(path)
            if fname in seen:
                continue  # first dir wins on a basename collision
            seen.add(fname)
            name = fname[:-3]  # strip .md
            kind = name.split("-", 1)[0]
            polarity = POLARITY.get(kind)
            if polarity is None:
                continue  # not a report we put on the timeline
            dm = _DATE.search(name)
            if not dm:
                continue
            date = dm.group(1)
            slug = name.replace(f"{kind}-", "", 1).replace(date, "").strip("-")
            try:
                with open(path, errors="ignore") as fh:
                    body = fh.read()
            except OSError:
                body = ""
            out.append(
                {
                    "date": date,
                    "title": _title(path),
                    "polarity": polarity,
                    "kind": kind,
                    "slug": slug,
                    "session_refs": _refs(body),
                    "file": fname,
                }
            )
    out.sort(key=lambda e: e["date"])
    return out


if __name__ == "__main__":
    rows = load()
    print(f"{len(rows)} report(s) from {', '.join(DIRS)}:")
    for e in rows:
        print(f"  {e['date']}  [{e['polarity']:8}] {e['title'][:70]}")
        if e["session_refs"]:
            print(f"             refs: {', '.join(e['session_refs'])}")
