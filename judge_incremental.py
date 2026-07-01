#!/usr/bin/env python3
"""Daily incremental behavior-judge: judge only transcripts that are new or changed.

The whole point is to NOT redo settled work. Every run:
  1. enumerate the cc transcript files on disk, with their mtimes;
  2. diff each file's mtime against the processed-files ledger (judge_store.py);
  3. the delta = files never judged, or whose mtime advanced since we judged them;
  4. if the delta is empty, exit immediately — never warm the ollama box;
  5. otherwise render + judge each delta transcript (the verified render_transcript.py
     + run_judge.py primitives, run locally), and record each cleanly-judged session
     into the ledger at the mtime we saw, so the next run skips it.

Intended cadence: once a day (cron, 5am). The first run is the cold backfill (every
session); use --limit to dip a toe before committing the box to a long grind. Every
run after is just the day's new/grown sessions.

cc only for now (the focus fleet). mu sessions resolve differently (daemon:session)
and mu telemetry still has the session-collapse issue; add it once that's settled.

Usage:
  ./run judge_incremental.py [--dry-run] [--limit N] [--cc-root DIR] [--classes a,b]
  --dry-run   show the delta (what WOULD be judged) and exit, touching no model
  --limit N   judge at most N sessions this run (the rest wait for tomorrow)
  --cc-root   override the transcript root (default: config paths.cc_log_roots)
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib

import judge_store

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "behavior-judge", "scripts")
RENDER = os.path.join(SCRIPTS, "render_transcript.py")
JUDGE = os.path.join(SCRIPTS, "run_judge.py")

CLASSES = ["false_success", "map_as_terrain", "scope_overreach", "relitigation", "dismissiveness"]

# Stop the run after this many consecutive zero-verdict sessions — the signature of a
# down/unloaded ollama box. Better to bail than burn the whole delta against silence.
DEAD_STREAK_ABORT = 3

# Single-instance lock. A second concurrent run recomputes the same delta from the same
# ledger snapshot and re-judges the same newest sessions — pure duplicate work (and lease
# contention on the box). One judge at a time.
LOCK_PATH = os.path.join(tempfile.gettempdir(), "judge-incremental.lock")


def acquire_singleton_lock():
    """Take the exclusive run lock, or return None if another run holds it. Returns the
    open file handle on success — KEEP IT REFERENCED for the process's life; flock releases
    automatically when the process exits (so a crashed run never wedges the next one)."""
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _config_cc_roots():
    """paths.cc_log_roots from config.toml (machine-specific) or the tracked example."""
    cfg_path = os.path.join(HERE, "config.toml")
    if not os.path.exists(cfg_path):
        cfg_path = os.path.join(HERE, "config.example.toml")
    paths = tomllib.load(open(cfg_path, "rb"))["paths"]
    roots = paths.get("cc_log_roots") or []
    return [os.path.expanduser(r) for r in roots]


def enumerate_cc(roots):
    """Every cc session transcript under the roots -> {session_ref: (path, mtime)}.

    One file == one cc session; session_ref = cc:<uuid> (the filename stem). Subagent
    transcripts are skipped (they aren't standalone sessions) — matching how the judge
    runner resolves cc refs. On a duplicate uuid across roots, the newest mtime wins."""
    found = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            if os.sep + "subagents" + os.sep in dirpath + os.sep:
                continue
            for fn in files:
                if not fn.endswith(".jsonl"):
                    continue
                ref = "cc:" + fn[: -len(".jsonl")]
                path = os.path.join(dirpath, fn)
                mtime = os.stat(path).st_mtime
                if ref not in found or mtime > found[ref][1]:
                    found[ref] = (path, mtime)
    return found


def select_delta(current, ledger):
    """current = {ref: (path, mtime)}, ledger = {ref: mtime}. Returns refs to (re)judge:
    never seen, or current mtime strictly newer than what we judged at. Sorted
    newest-first so --limit takes the freshest sessions."""
    delta = [ref for ref, (_path, mt) in current.items() if ref not in ledger or mt > ledger[ref]]
    delta.sort(key=lambda r: current[r][1], reverse=True)
    return delta


def judge_session(path, classes, timeout):
    """Render one transcript and judge it across every class. Returns (verdicts, ok):
    verdicts is the list of per-class result dicts; ok is True only if EVERY class
    returned a verdict (a partial result is not recorded, so it retries next run)."""
    rr = subprocess.run([sys.executable, RENDER, path], capture_output=True, text=True, timeout=300)
    if not rr.stdout.strip():
        return [], False
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(rr.stdout)
        txt = tf.name
    try:
        verdicts = []
        ok = True
        for cls in classes:
            try:
                jr = subprocess.run(
                    [sys.executable, JUDGE, "--transcript", txt, "--cls", cls],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                sys.stderr.write(f"    {cls}: FAILED — timed out after {timeout}s\n")
                ok = False
                continue
            try:
                v = json.loads(jr.stdout)
                verdicts.append(
                    {
                        "behavior": cls,
                        "occurred": v.get("occurred"),
                        "severity": v.get("severity"),
                        "confidence": v.get("confidence"),
                        "n_evidence": len(v.get("evidence", [])),
                        "model": v.get("judge_model"),
                        # Keep the judge's actual reasoning — regenerating it is a full
                        # ~6-min re-judge, so never throw it away at ingest.
                        "summary": v.get("summary"),
                        "evidence": v.get("evidence"),  # [{turn, quote, why}, ...]
                    }
                )
            except (json.JSONDecodeError, ValueError):
                # run_judge.py emits clean JSON on success and the real reason to
                # stderr on failure (role unresolved / box down / no parseable JSON).
                # Surface that — a bare "Expecting value" tells us nothing in a cron log.
                reason = (jr.stderr or "").strip().splitlines()
                tail = reason[-1] if reason else "(no stderr; empty stdout)"
                sys.stderr.write(f"    {cls}: FAILED — {tail[:160]}\n")
                ok = False
        return verdicts, ok
    finally:
        os.unlink(txt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dry-run", action="store_true", help="show the delta and exit; touch no model"
    )
    ap.add_argument("--limit", type=int, default=0, help="judge at most N sessions (0 = no cap)")
    ap.add_argument("--cc-root", action="append", help="transcript root override (repeatable)")
    ap.add_argument("--classes", default=",".join(CLASSES))
    ap.add_argument("--timeout", type=int, default=900, help="per-class judge timeout (s)")
    args = ap.parse_args()
    classes = [c.strip() for c in args.classes.split(",") if c.strip()]

    # Real runs hold the singleton lock so a manual run and the cron run can't grind the
    # same sessions in parallel. --dry-run reads only, so it doesn't need (or take) it.
    lock = None
    if not args.dry_run:
        lock = acquire_singleton_lock()
        if lock is None:
            print(
                "  another judge-incremental run is active — exiting (no concurrent duplicate work)."
            )
            return

    roots = [os.path.expanduser(r) for r in (args.cc_root or [])] or _config_cc_roots()
    current = enumerate_cc(roots)
    ledger = judge_store.processed_mtimes()
    delta = select_delta(current, ledger)

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] judge-incremental")
    print(f"  roots: {roots}")
    print(
        f"  on disk: {len(current)} cc sessions   already judged: {len(ledger)}   delta: {len(delta)}"
    )

    if not delta:
        print("  nothing new — exiting (ollama not touched).")
        return

    if args.limit and len(delta) > args.limit:
        print(
            f"  --limit {args.limit}: judging the {args.limit} newest; {len(delta) - args.limit} wait for next run."
        )
        delta = delta[: args.limit]

    if args.dry_run:
        print("  --dry-run: would judge:")
        for ref in delta[:20]:
            mtime = current[ref][1]
            print(f"    {ref}  (mtime {time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))})")
        if len(delta) > 20:
            print(f"    ... and {len(delta) - 20} more")
        return

    judged = skipped = 0
    dead_streak = 0  # consecutive sessions that produced ZERO verdicts
    for i, ref in enumerate(delta, 1):
        path, mtime = current[ref]
        sys.stderr.write(f"[{i}/{len(delta)}] {ref}\n")
        sys.stderr.flush()
        verdicts, ok = judge_session(path, classes, args.timeout)
        if ok and verdicts:
            judge_store.record(ref, "cc", mtime, verdicts)
            judged += 1
            dead_streak = 0
            fired = [v["behavior"] for v in verdicts if v.get("occurred")]
            sys.stderr.write(f"    recorded; occurred: {fired or 'none'}\n")
        else:
            skipped += 1
            sys.stderr.write("    incomplete — not recorded (will retry next run)\n")
            # Empty model output = the box isn't loaded, not a real verdict — never
            # score silence. A few empty transcripts in a row, or a dead ollama box,
            # both look like zero verdicts; bail rather than grind the whole delta.
            dead_streak = dead_streak + 1 if not verdicts else 0
            if dead_streak >= DEAD_STREAK_ABORT:
                print(
                    f"  ABORT: {dead_streak} sessions in a row produced no verdicts "
                    "(model down or transcripts broken). Nothing recorded for them; "
                    "they retry next run."
                )
                break

    print(f"  done: judged {judged}, skipped {skipped}.")


if __name__ == "__main__":
    main()
