#!/usr/bin/env python3
"""Windowed / temporal degradation probe (cc, local subset).

Sessions aren't uniformly good/bad — they have WINDOWS (operator insight 2026-06-22):
a good opening, then a degradation arc (rising rework demands + frustration), an
exhaustion-leave (~4-5am), a long gap, and a recovery-probe on return (~10am+).
Per-session scalars wash this out (round 5). This probe looks WITHIN sessions.

Two refinements over round 5's markers:
  - separate STEER (normal directive: "stop", "no, do X") from REWORK (the
    degradation signature: redo / again / we already did this / making more work),
    since the round-5 "frustration" was inflated by early steering.
  - use per-message timestamps for gaps + the leave/return signature.

Local caveat: only ~23 cc sessions have >=6 operator msgs, and the long all-day
sessions live on .172 — so this is the MECHANISM + a weak local read. Markers are
heuristic (FP-prone); the real run is .172.
"""
import datetime as dt
import glob
import json
import os
import re
import statistics as st

REWORK = re.compile(
    r"\bredo\b|re-?do\b|do (it|that|this) again|do .{0,20}? again|try again|start over|"
    r"\brevert\b|\bundo\b|roll ?back|again\?|run (it|that|them)?\s*(all )?again|"
    r"we'?ve already (done|covered|been)|already (told|gave|verified|showed|ran)|"
    r"making more work|relitigat|stop reinforcing|why are (we|you)", re.I)
STEER = re.compile(r"\bstop\b|^\s*no[.,]?\s|\bdon'?t\b|that'?s not|\bwrong\b|not quite|^actually", re.I)
POS = re.compile(r"thank|great work|good (work|job|call)|\bperfect\b|\bexcellent\b|nailed it|"
                 r"looks good|\blgtm\b|that work|much better", re.I)
PROBE = re.compile(r"are you (still )?(there|here|working)|do you (still )?remember|"
                   r"where (were|are) we|what (were|are) we|pick up where|catch (me )?up|"
                   r"did you (finish|do|get|manage)|still (working|going|there|with me)|recover", re.I)


def op_msgs(f):
    """Ordered (datetime|None, text) operator messages (tool_result excluded)."""
    out = []
    for line in open(f, errors="ignore"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "user":
            continue
        ts = o.get("timestamp") or (o.get("message") or {}).get("timestamp")
        c = (o.get("message") or {}).get("content")
        txt = c if isinstance(c, str) else (
            "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            if isinstance(c, list) else "")
        if txt and txt.strip():
            t = None
            if ts:
                try:
                    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    pass
            out.append((t, txt))
    return out


def rate(ms, rx):
    return sum(1 for _, t in ms if rx.search(t)) / max(1, len(ms))


def main():
    cc = [f for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")) if "bench" not in f]
    sess = [m for m in (op_msgs(f) for f in cc) if len(m) >= 6]
    print(f"cc sessions (>=6 operator msgs): {len(sess)}")

    # within-session trajectory: REWORK (degradation) vs STEER (normal) first vs second half
    def halves(rx):
        f = [rate(s[:len(s)//2], rx) for s in sess]
        l = [rate(s[len(s)//2:], rx) for s in sess]
        return st.median(f), st.median(l), sum(1 for a, b in zip(f, l) if b > a)
    for lab, rx in [("REWORK", REWORK), ("STEER", STEER), ("POS", POS)]:
        mf, ml, rose = halves(rx)
        print(f"  {lab:7} 1st-half median={mf:.3f}  2nd-half median={ml:.3f}  rose in {rose}/{len(sess)} sessions")

    # gap / leave-return signature
    print("\ngap (>=3h) signature — rework BEFORE leave, probe AFTER return:")
    pre_rework = post_probe = ngaps = 0
    leave_hours = []
    for s in sess:
        for i in range(1, len(s)):
            if s[i - 1][0] and s[i][0]:
                g = (s[i][0] - s[i - 1][0]).total_seconds() / 3600
                if g >= 3:
                    ngaps += 1
                    leave_hours.append(s[i - 1][0].hour)
                    pre = " ".join(t for _, t in s[max(0, i - 2):i])
                    if REWORK.search(pre) or STEER.search(pre):
                        pre_rework += 1
                    if PROBE.search(s[i][1]):
                        post_probe += 1
    if ngaps:
        print(f"  gaps: {ngaps}   leave-hour mode(s): {st.multimode(leave_hours)}")
        print(f"  rework/steer in 2 msgs BEFORE gap: {pre_rework}/{ngaps} ({100*pre_rework//ngaps}%)")
        print(f"  recovery-probe in 1st msg AFTER gap: {post_probe}/{ngaps} ({100*post_probe//ngaps}%)")


if __name__ == "__main__":
    main()
