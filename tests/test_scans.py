"""DS1: frustration_scan ported onto the `ev` view (scans.py). Hermetic — builds
a synthetic both-fleets event log of user_message rows and asserts marker
detection, ET weekend/weekday bucketing, the canonical session key, and the
incidence/rate math the legacy scanner produced.

Regression anchor: two session files in the SAME mu daemon dir (session-1 +
session-2) must be counted as TWO sessions, not merged — the bug that grouping
by `daemon` instead of the `daemon:session_id` `session` key introduced."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402
import scans  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _write(path, session_id, rows):
    """rows: (when, content) or (when, content, meta) user messages."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for i, row in enumerate(rows, 1):
            when, content = row[0], row[1]
            payload = {"kind": "user_message", "content": content}
            if len(row) > 2 and row[2]:
                payload["meta"] = True  # harness-injected, excluded from operator scans
            f.write(
                json.dumps(
                    {
                        "id": i,
                        "session_id": session_id,
                        "timestamp_unix_ms": _ms(when),
                        "actor": {"kind": "user"},
                        "payload": payload,
                    }
                )
                + "\n"
            )


# A Saturday and a Monday in ET — exercise both weekend + weekday windows.
SAT = datetime(2026, 6, 13, 14, 0, tzinfo=ET)  # weekend  -> W-jun13
MON = datetime(2026, 6, 15, 12, 0, tzinfo=ET)  # weekday  -> wd-jun15


def _connect(tmp):
    ev = os.path.join(tmp, "events")
    # daemon "d1" hosts TWO sessions in ONE dir (the multi-file reality).
    _write(
        os.path.join(ev, "d1", "session-1.jsonl"),
        "session-1",
        [
            (SAT, "please stop doing that"),  # \bstop\b
            (SAT, "why are you doing this"),  # why are (we|you)
            (SAT, "all good here"),  # no marker
            # harness-injected (meta) — full of markers, but MUST be excluded so it
            # neither inflates hits nor the denominator. If the meta filter breaks,
            # session-1 reads 4 msgs / 3 hits and the assertions below fail.
            (SAT, "<task-notification> stop wrong again broken </task-notification>", True),
        ],
    )
    _write(
        os.path.join(ev, "d1", "session-2.jsonl"),
        "session-2",
        [(SAT, "stop it now"), (SAT, "ok thanks")],  # 1 marker
    )
    # daemon "d2": 1 user msg -> excluded (<2).
    _write(os.path.join(ev, "d2", "session-1.jsonl"), "session-1", [(SAT, "hi")])
    # cc session: 2 user msgs, 0 markers, on a weekday.
    _write(
        os.path.join(tmp, "cc-events", "claude-code", "ccuuid.jsonl"),
        "ccuuid",
        [(MON, "hello"), (MON, "thanks for the help")],
    )
    return engine.connect(
        sources=[
            (os.path.join(ev, "*", "*.jsonl"), "mu", engine._MU_DAEMON, engine._MU_SESSION),
            (
                os.path.join(tmp, "cc-events", "*", "*.jsonl"),
                "cc",
                engine._CC_DAEMON,
                engine._CC_SESSION,
            ),
        ]
    )


class TestFrustrationScan(unittest.TestCase):
    def test_sessions_in_one_daemon_are_not_merged(self):
        # the regression: d1/session-1 and d1/session-2 are TWO sessions, never
        # one merged "d1". (The legacy daemon-key bug merged them.)
        with tempfile.TemporaryDirectory() as tmp:
            con = _connect(tmp)
            _hit, all_rows, _tot = scans.scan_frustration(con)
        refs = {r[0] for r in all_rows}
        self.assertEqual(refs, {"mu:d1:session-1", "mu:d1:session-2", "cc:ccuuid"})
        self.assertNotIn("mu:d1", refs)  # no merged daemon-only session

    def test_markers_windows_fleets_and_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = _connect(tmp)
            hit_rows, all_rows, totals = scans.scan_frustration(con)

        by_ref = {r[0]: r for r in hit_rows}
        # session-1: 2 hits / 3 msgs;  session-2: 1 hit / 2 msgs — counted apart.
        self.assertEqual(by_ref["mu:d1:session-1"][2], 2)  # hits
        self.assertEqual(by_ref["mu:d1:session-1"][3], 3)  # n_user
        self.assertEqual(by_ref["mu:d1:session-2"][2], 1)
        self.assertEqual(by_ref["mu:d1:session-2"][3], 2)
        self.assertTrue(by_ref["mu:d1:session-1"][1].startswith("W-"))  # weekend

        # cc is keyed by session_id (the uuid) and weekday-bucketed, no hits.
        cc_row = next(r for r in all_rows if r[0] == "cc:ccuuid")
        self.assertTrue(cc_row[1].startswith("wd-"))
        self.assertEqual(cc_row[4], 0)

        # weekend window denominators: 2 sessions / 5 msgs / 3 hits -> 60/100msg.
        wk = next(v for k, v in totals.items() if k.startswith("W-"))
        self.assertEqual(wk[0], 2)  # sessions (both d1 files)
        self.assertEqual(wk[2], 5)  # user msgs (3 + 2)
        self.assertEqual(wk[3], 3)  # hits (2 + 1)
        self.assertAlmostEqual(100 * wk[3] / wk[2], 60.0, places=3)

    def test_explicit_window_overrides_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = _connect(tmp)
            explicit = [scans.parse_window("2026-06-13T00:00..2026-06-14T00:00=INC")]
            _hit, all_rows, totals = scans.scan_frustration(con, explicit=explicit)
        d1 = next(r for r in all_rows if r[0] == "mu:d1:session-1")
        self.assertEqual(d1[1], "INC")
        self.assertIn("INC", totals)


class TestSentimentScan(unittest.TestCase):
    """DS3 signed scan: net = pos - neg, meta-filtered, keyed by session_ref."""

    def test_signed_net_per_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = _connect(tmp)
            _hit, all_rows, _tot = scans.scan_sentiment(con)
        # all_rows: (ref, win, first_ts, n_user, pos, neg, net, ending)
        by_ref = {r[0]: r for r in all_rows}
        # session-1: "stop"+"why are you" = 2 neg, 0 pos -> net -2 (meta msg excluded)
        self.assertEqual(by_ref["mu:d1:session-1"][4:7], (0, 2, -2))
        self.assertEqual(by_ref["mu:d1:session-1"][3], 3)  # n_user (meta excluded)
        # session-2: "stop" (neg) + "ok thanks" (pos "thank") -> balanced, net 0
        self.assertEqual(by_ref["mu:d1:session-2"][4:7], (1, 1, 0))
        # cc: "thanks for the help" (1 pos), no neg -> net +1 (positive sentiment)
        self.assertEqual(by_ref["cc:ccuuid"][4:7], (1, 0, 1))


if __name__ == "__main__":
    unittest.main()
