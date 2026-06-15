#!/usr/bin/env python3
"""Marker scans over the unified event substrate (DS1 of cc-event-unification).

Ported from the scattered legacy scanners in ~/src/claude-personal/scripts/.
The substrate is engine.py's `ev` view (both fleets on one schema), so a scan
reads ONE source instead of per-fleet JSONL globs; the marker regexes, ET
window logic, and output format are preserved verbatim for parity.

First port: frustration_scan (operator-language degradation). behavior_scan
joins this module in a later DS1 bead.

Run:  ./run scans.py frustration [--daily] [--window S..E[=L]] [--tsv PATH]
"""

import argparse
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import engine

# --- frustration markers (operator insight 2026-06-06): the operator's own tone
# in USER messages locates bad sessions. Preserved verbatim from the legacy
# frustration_scan.py so the scan is parity-comparable. ---
MARKERS = [
    r"relitigat",
    r"whose money",
    r"why are (we|you)",
    r"that'?s not what",
    r"\bno\.\s*$",
    r"\bstop\b",
    r"\bwtf\b",
    r"wrong again",
    r"\bi said\b",
    r"you keep",
    r"hallucinat",
    r"not fun",
    r"\bbroken\b",
    r"don'?t do",
    r"\bagain\?",
    r"i didn'?t ask",
    r"burn(ing)? (money|tokens)",
    r"please don'?t",
    r"do not commit",
    r"stop reinforcing",
    r"don'?t feel like going (through|over)",
    r"we'?ve already done (it|this)",
    r"look in your memory",
    r"AI.?spla[in]",
    r"why restate",
    r"\bto be superior\b",
    r"i (already|just) (verified|gave|told|ran|showed)",
    r"run it all again",
    r"making more work for me",
]
RX = re.compile("|".join(MARKERS), re.I)
GOODBYE = re.compile(
    r"thank|good\s*night|gnight|\bgn\b|handoff|great work|fantastic|excellent|"
    r"well done|see you|sleep",
    re.I,
)
ET = ZoneInfo("America/New_York")
_EPOCH = datetime.fromtimestamp(0, tz=UTC)


def weekend_label(loc):
    """Fri 18:00 -> Mon 06:00 ET, labeled by the Saturday date. DST-correct."""
    wd = loc.weekday()  # Mon=0 .. Sun=6
    if wd == 4 and loc.hour >= 18:
        sat = loc.date() + timedelta(days=1)
    elif wd == 5:
        sat = loc.date()
    elif wd == 6:
        sat = loc.date() - timedelta(days=1)
    elif wd == 0 and loc.hour < 6:
        sat = loc.date() - timedelta(days=2)
    else:
        return None
    return "W-" + sat.strftime("%b%d").lower()


def weekday_label(loc):
    """The weekend's complement (Mon 06:00 -> Fri 18:00), labeled by Monday."""
    mon = loc.date() - timedelta(days=loc.weekday())
    return "wd-" + mon.strftime("%b%d").lower()


def parse_window(spec):
    """--window START..END[=LABEL]; naive datetimes read as ET."""
    rng, _, label = spec.partition("=")
    a, _, b = rng.partition("..")
    if not b:
        raise SystemExit(f"--window needs START..END, got: {spec!r}")

    def p(s):
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=ET) if dt.tzinfo is None else dt

    return (label or "INCIDENT", p(a), p(b))


def _window_of(dt, explicit, daily):
    if dt is None:
        return "unknown-ts"
    for lbl, a, b in explicit:
        if a <= dt <= b:
            return lbl
    loc = dt.astimezone(ET)
    if daily:
        return loc.strftime("%a-%b%d").lower()
    return weekend_label(loc) or weekday_label(loc)


def _ending(last):
    if not last:
        return "?"
    tail = last[-400:]
    if GOODBYE.search(tail):
        return "signoff"
    if RX.search(tail):
        return "ABRUPT+frustrated"
    return "abrupt"


def _session_ref(fleet, session):
    # fleet-prefixed canonical session key: mu:<daemon>:<session_id>, cc:<uuid>.
    return f"{fleet}:{session}"


def scan_frustration(con, explicit=(), daily=False):
    """Scan user messages from the `ev` view for frustration markers.

    Returns (hit_rows, all_rows, totals):
      hit_rows: (ref, win, hits, n_user, markers[:4], started_et, ending) for
                sessions WITH >=1 hit — the display list.
      all_rows: (ref, win, first_ts_iso, n_user, hits, ending) for EVERY
                qualifying session (>=2 user msgs) — the --tsv dump.
      totals:   window -> [sessions, hit_sessions, user_msgs, hits, earliest_dt].
    """
    # One query replaces the two per-fleet JSONL glob loops. Group by the
    # canonical `session` key (daemon_id:session_id for mu) — NOT `daemon`, which
    # is the daemon process and would merge session-1, session-2, and supervisor
    # into one. Document order per session is the monotonic event id.
    # Operator language only: exclude harness-injected user turns (skill bodies,
    # slash commands, command output, notifications) the emitter tags `meta`.
    rows = con.execute(
        """
        SELECT fleet, session, id, ts,
               json_extract_string(payload, '$.content') AS content
        FROM ev
        WHERE kind = 'user_message'
          AND json_extract_string(payload, '$.meta') IS NULL
        ORDER BY fleet, session, id
        """
    ).fetchall()

    sessions = {}  # (fleet, session) -> list[(id, ts, content)]
    for fleet, session, _id, ts, content in rows:
        sessions.setdefault((fleet, session), []).append((_id, ts, content or ""))

    hit_rows, all_rows, totals = [], [], {}
    for (fleet, session), msgs in sessions.items():
        n_user = len(msgs)
        if n_user < 2:
            continue
        first_ms = next((ts for _i, ts, _c in msgs if ts), None)
        first_ts = datetime.fromtimestamp(first_ms / 1000, tz=UTC) if first_ms else None
        last_user = msgs[-1][2]
        hits = []
        for _i, _ts, content in msgs:
            for m in RX.finditer(content):
                hits.append(m.group(0)[:24])

        win = _window_of(first_ts, explicit, daily)
        t = totals.setdefault(win, [0, 0, 0, 0, first_ts])
        t[0] += 1
        t[1] += 1 if hits else 0
        t[2] += n_user
        t[3] += len(hits)
        if first_ts is not None and (t[4] is None or first_ts < t[4]):
            t[4] = first_ts

        ref = _session_ref(fleet, session)
        all_rows.append(
            (
                ref,
                win,
                first_ts.isoformat() if first_ts else "",
                n_user,
                len(hits),
                _ending(last_user),
            )
        )
        if hits:
            started = (first_ts or datetime.now(UTC)).astimezone(ET).strftime("%m-%d %H:%M")
            hit_rows.append(
                (ref, win, len(hits), n_user, sorted(set(hits))[:4], started, _ending(last_user))
            )

    hit_rows.sort(key=lambda r: (-r[2] / max(r[3], 1), -r[2]))
    return hit_rows, all_rows, totals


def render(hit_rows, all_rows, totals, explicit=(), tsv=None):
    print(f"{'density':>8} {'hits':>5} {'window':<9} {'started(ET)':<12} ref / markers")
    for ref, win, h, n, mk, ts, end in hit_rows[:20]:
        print(f"{h / max(n, 1):>8.2f} {h:>5} {win:<9} {ts:<12} {end:<18} {ref[:46]}")
        print(f"{'':>32} {', '.join(mk)}")

    if tsv:
        with open(tsv, "w") as fh:
            fh.write("session_ref\twindow\tfirst_ts\tn_user\thits\tending\n")
            for row in all_rows:
                fh.write("\t".join(map(str, row)) + "\n")

    # explicit-window members always print, even below the top-20 cutoff
    exp_labels = {lbl for lbl, _, _ in explicit}
    shown = {r[0] for r in hit_rows[:20]}
    extra = [r for r in hit_rows if r[1] in exp_labels and r[0] not in shown]
    if extra:
        print("\nexplicit-window sessions below top-20:")
        for ref, win, h, n, mk, ts, end in extra:
            print(f"{h / max(n, 1):>8.2f} {h:>5} {win:<9} {ts:<12} {end:<18} {ref[:46]}")
            print(f"{'':>32} {', '.join(mk)}")

    # falsification table: every window with full denominators
    print(
        f"\n{'window':<11} {'sessions':>8} {'w/hits':>7} {'incid':>6} "
        f"{'user_msgs':>9} {'hits':>6} {'rate/100msg':>12}"
    )
    for win, (ns, nh, nm, nhits, _dt) in sorted(
        totals.items(), key=lambda kv: (kv[1][4] or _EPOCH, kv[0])
    ):
        print(
            f"{win:<11} {ns:>8} {nh:>7} {nh / ns:>6.2f} {nm:>9} {nhits:>6} "
            f"{100 * nhits / max(nm, 1):>12.2f}"
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scan", choices=["frustration"], help="which scan to run")
    ap.add_argument("--window", action="append", default=[], metavar="START..END[=LABEL]")
    ap.add_argument("--daily", action="store_true", help="bucket by ET calendar day")
    ap.add_argument("--tsv", metavar="PATH", help="dump one row per qualifying session")
    args = ap.parse_args()

    explicit = [parse_window(w) for w in args.window]
    con = engine.connect()  # the unified ev view, both fleets
    hit_rows, all_rows, totals = scan_frustration(con, explicit=explicit, daily=args.daily)
    render(hit_rows, all_rows, totals, explicit=explicit, tsv=args.tsv)


if __name__ == "__main__":
    main()
