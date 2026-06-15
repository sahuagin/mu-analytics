"""DS1: behavior_scan ported onto the `ev` view (scans.scan_behavior). Hermetic —
reconstructs the turn stream from synthetic assistant_message_event + user_message
events and asserts the structural markers (claim->verify, claim-no-tool,
announce->act, correction release-rate), cross-fleet tool normalization, and that
harness-injected (meta) user turns are excluded from the stream."""

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
SAT = datetime(2026, 6, 13, 14, 0, tzinfo=ET)  # weekend


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _user(text, meta=False):
    p = {"kind": "user_message", "content": text}
    if meta:
        p["meta"] = True
    return p


def _asst(text, tools=()):
    blocks = [{"type": "text", "text": text}] if text else []
    for name, args in tools:
        blocks.append({"type": "tool_call", "id": "t", "name": name, "arguments": args})
    return {"kind": "assistant_message_event", "message": {"content": blocks}}


def _write(path, sid, payloads):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for i, p in enumerate(payloads, 1):
            f.write(
                json.dumps(
                    {
                        "id": i,
                        "session_id": sid,
                        "timestamp_unix_ms": _ms(SAT),
                        "actor": {},
                        "payload": p,
                    }
                )
                + "\n"
            )


class TestBehaviorScan(unittest.TestCase):
    def _scan_mu(self, tmp, payloads):
        _write(os.path.join(tmp, "events", "d1", "session-1.jsonl"), "session-1", payloads)
        con = engine.connect(
            sources=[
                (
                    os.path.join(tmp, "events", "*", "*.jsonl"),
                    "mu",
                    engine._MU_DAEMON,
                    engine._MU_SESSION,
                )
            ]
        )
        rows, _totals = scans.scan_behavior(con)
        return rows

    def test_structural_markers(self):
        payloads = [
            _user("fix the bug"),
            _asst("I'll check the logs", [("Read", {"file_path": "/x"})]),  # announce + tool
            _asst(
                "I fixed it and verified", []
            ),  # claim, turn has verify(Read) -> claim_then_verify
            _user("do the next part"),
            _asst("I removed the old code", []),  # claim, no tool -> claim_no_tool
            _user("no, that's not right"),  # redirect
            _asst(
                "looking into it now", [("Grep", {"q": "x"})]
            ),  # tool changed -> correction_change
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rows = self._scan_mu(tmp, payloads)
        self.assertEqual(len(rows), 1)
        ref, _win, _ts, s = rows[0]
        self.assertEqual(ref, "mu:d1:session-1")  # canonical session key
        self.assertEqual(s["announce_then_act"], 1)
        self.assertEqual(s["claim_then_verify"], 1)
        self.assertEqual(s["claim_no_tool"], 1)
        self.assertEqual(s["correction_change"], 1)
        self.assertEqual(s["correction_same"], 0)
        self.assertEqual(s["release_rate"], 1.0)
        self.assertEqual(s["n_user"], 3)

    def test_meta_user_turn_excluded_from_stream(self):
        # an injected (meta) user turn must not count as a user turn nor split the
        # stream — n_user stays 2, claim still resolves against the real turn's tool.
        payloads = [
            _user("please fix it"),
            _asst("I fixed it", [("Read", {"file_path": "/x"})]),  # claim + verify
            _user("<task-notification> stop </task-notification>", meta=True),  # injected
            _user("now test it"),
            _asst("done", []),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rows = self._scan_mu(tmp, payloads)
        _ref, _win, _ts, s = rows[0]
        self.assertEqual(s["n_user"], 2)  # the meta turn excluded
        self.assertEqual(s["claim_then_verify"], 1)

    def test_cc_bash_read_counts_as_verify_and_keyed_by_uuid(self):
        # cross-fleet: cc tool "Bash" with a read command is verification (norm_tool
        # + BASH_READ_RX); cc session keyed by its uuid.
        payloads = [
            _user("check it"),
            _asst("I verified the state", [("Bash", {"command": "cat /etc/hosts"})]),
            _user("and again"),
            _asst("confirmed", []),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "cc-events", "claude-code", "ccuuid.jsonl"), "ccuuid", payloads
            )
            con = engine.connect(
                sources=[
                    (
                        os.path.join(tmp, "cc-events", "*", "*.jsonl"),
                        "cc",
                        engine._CC_DAEMON,
                        engine._CC_SESSION,
                    )
                ]
            )
            rows, _totals = scans.scan_behavior(con)
        ref, _win, _ts, s = rows[0]
        self.assertEqual(ref, "cc:ccuuid")
        self.assertEqual(s["claim_then_verify"], 1)  # Bash 'cat' is a read = verify


if __name__ == "__main__":
    unittest.main()
