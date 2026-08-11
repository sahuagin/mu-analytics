#!/usr/bin/env python3
"""Lexical behavior-signature detectors over the `ev` view — per-TURN, both fleets.

The syntactic half of the degradation-signature sweep (the semantic half is the
behavior-judge's v2 classes). Two detectors, both shingle-based (5-word shingles,
Jaccard), both operating at turn granularity so every hit carries its own
timestamp and its own context size — session-level binning proved useless here:
a multi-day session bins days away from the behavior it contains.

  scan_repetition : within one session, each substantial assistant turn's best
      Jaccard against the session's prior turns. Flags near-verbatim re-emission
      (loop re-explanations, re-pasted status blocks). Validated on the archive:
      repetition concentrates in LOW-context turns (<50k), not long sessions.
  closing_pairs   : across sessions, the last substantial assistant turn of each
      (session, ET calendar day), pairwise. What this finds is TEMPLATE reuse
      (skill/tool boilerplate, repeated error prose) — validated finding: the
      operator-observed "same wrap-up voice" similarity is stylistic, carries
      ~zero lexical overlap at turn granularity, and belongs to the judge's
      performative_closing class, not to this detector. High-sim pairs here that
      are NOT known templates are the anomaly worth reading.

Read-only; stdlib + the ev view. Run: ./run signatures.py   (smoke over the archive)
"""

import json
import re
from datetime import UTC, datetime

from scans import ET

WORD_RX = re.compile(r"[a-z0-9']+")
SHINGLE_K = 5
MIN_TURN_WORDS = 15  # below this a turn has too few shingles to compare fairly
MIN_CLOSING_WORDS = 30  # day-closings shorter than this are pointers/acks, not prose
MIN_CLOSING_SESSION_TURNS = 2  # a one-shot worker (judge call, single-answer dispatch)
# has no "closing" distinct from its only output — the archive holds thousands of
# these and their templated outputs would dominate every cluster
REPEAT_THRESHOLD = 0.5
PAIR_THRESHOLD = 0.25
PRIOR_WINDOW = 30  # compare a turn against at most this many prior turns


def _shingles(text):
    w = WORD_RX.findall(text.lower())
    return frozenset(" ".join(w[i : i + SHINGLE_K]) for i in range(len(w) - SHINGLE_K + 1)), len(w)


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def assistant_turns(con):
    """{session_ref: [(ts_ms, text, ctx_tokens)]} — every assistant turn with prose,
    in event order. ctx is the turn's OWN context size (cc: usage input+cache; mu has
    no per-turn usage on assistant events -> 0, ctx bands are cc-only for now)."""
    rows = con.execute(
        """
        SELECT fleet, session, id, ts, payload FROM ev
        WHERE kind = 'assistant_message_event'
        ORDER BY fleet, session, id
        """
    ).fetchall()
    out = {}
    for fleet, session, _id, ts, payload in rows:
        p = json.loads(payload) if isinstance(payload, str) else payload
        msg = p.get("message") or {}
        text = "\n".join(
            b.get("text", "")
            for b in (msg.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        if not text or not ts:
            continue
        u = msg.get("usage") or {}
        ctx = sum(
            u.get(k) or 0
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        out.setdefault(f"{fleet}:{session}", []).append((ts, text, ctx))
    return out


def scan_repetition(turns_by_ref):
    """Per-turn repetition records: [{ref, ts, et_hour, et_date, ctx, best_sim, repeat}].
    One record per substantial turn (>= MIN_TURN_WORDS); repeat = best Jaccard vs the
    session's prior substantial turns >= REPEAT_THRESHOLD."""
    records = []
    for ref, turns in turns_by_ref.items():
        prior = []
        for ts, text, ctx in turns:
            sh, nw = _shingles(text)
            if nw < MIN_TURN_WORDS:
                continue
            best = max((_jaccard(sh, p) for p in prior[-PRIOR_WINDOW:]), default=0.0)
            prior.append(sh)
            loc = datetime.fromtimestamp(ts / 1000, tz=UTC).astimezone(ET)
            records.append(
                {
                    "ref": ref,
                    "ts": ts,
                    "et_hour": loc.hour,
                    "et_date": loc.strftime("%Y-%m-%d"),
                    "ctx": ctx,
                    "best_sim": round(best, 3),
                    "repeat": best >= REPEAT_THRESHOLD,
                }
            )
    return records


def day_closings(turns_by_ref):
    """[(ref, et_date, et_hour, shingleset, preview)] — the last substantial assistant
    turn of each (session, ET day). Multi-day sessions contribute one closing per day."""
    closings = []
    for ref, turns in turns_by_ref.items():
        by_day = {}
        n_substantial = 0
        for ts, text, _ctx in turns:
            sh, nw = _shingles(text)
            if nw < MIN_CLOSING_WORDS:
                continue
            n_substantial += 1
            loc = datetime.fromtimestamp(ts / 1000, tz=UTC).astimezone(ET)
            by_day[loc.strftime("%Y-%m-%d")] = (loc.hour, sh, text)
        if n_substantial < MIN_CLOSING_SESSION_TURNS:
            continue
        for day, (hour, sh, text) in by_day.items():
            closings.append((ref, day, hour, sh, text[:120].replace("\n", " ")))
    return closings


def closing_pairs(closings, threshold=PAIR_THRESHOLD):
    """Cross-session day-closing pairs with Jaccard >= threshold, most-similar first:
    [{sim, a_ref, a_date, a_hour, b_ref, b_date, b_hour, a_preview, b_preview}].
    Same-session pairs (one long session closing similarly on two days) are excluded —
    self-similarity across days is scan_repetition's business."""
    pairs = []
    for i in range(len(closings)):
        ri, di, hi, si, pi = closings[i]
        for j in range(i + 1, len(closings)):
            rj, dj, hj, sj, pj = closings[j]
            if ri == rj:
                continue
            sim = _jaccard(si, sj)
            if sim >= threshold:
                pairs.append(
                    {
                        "sim": round(sim, 3),
                        "a_ref": ri,
                        "a_date": di,
                        "a_hour": hi,
                        "b_ref": rj,
                        "b_date": dj,
                        "b_hour": hj,
                        "a_preview": pi,
                        "b_preview": pj,
                    }
                )
    pairs.sort(key=lambda p: -p["sim"])
    return pairs


def _smoke():
    import engine

    con = engine.connect()
    turns = assistant_turns(con)
    rec = scan_repetition(turns)
    n_rep = sum(1 for r in rec if r["repeat"])
    print(f"sessions: {len(turns)}  substantial turns: {len(rec)}  repeats: {n_rep}")
    cl = day_closings(turns)
    pairs = closing_pairs(cl)
    print(f"day-closings: {len(cl)}  cross-session pairs >= {PAIR_THRESHOLD}: {len(pairs)}")
    for p in pairs[:5]:
        print(
            f"  {p['sim']:.2f}  {p['a_ref'][:20]} {p['a_date']}  ~  {p['b_ref'][:20]} {p['b_date']}"
        )


if __name__ == "__main__":
    _smoke()
