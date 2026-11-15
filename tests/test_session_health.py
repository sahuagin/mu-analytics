"""mu-analytics-session-health-recover-m296l: per-session health rows from
mu-core's recovery diagnosis, cached by file size and mtime, and the recover
worklist built from them."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import session_health as sh  # noqa: E402

NOW = 1_800_000_000.0
DAY_NS = 86_400 * 10**9


def _row(status, ref="d1:session-1", age_d=10.0, **kw):
    daemon, session = ref.split(":")
    row = {
        "status": status,
        "session_id": session,
        "event_count": 10,
        "last_event_id": 10,
        "gaps": [],
        "torn_lines": 0,
        "fork_event_id": None,
        "damage": None,
        "open_calls": [],
        "predecessor_session_id": None,
        "detail": None,
        "file": f"/a/m1/events/{daemon}/{session}.jsonl",
        "machine": "m1",
        "daemon": daemon,
        "session_ref": ref,
        "bytes": 100,
        "mtime_ns": int(NOW * 10**9 - age_d * DAY_NS),
    }
    row.update(kw)
    return row


class Refresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root / "archive"
        self.log = self.root / "m1" / "events" / "d1" / "session-1.jsonl"
        self.log.parent.mkdir(parents=True)
        self.log.write_text("{}\n")
        self.cache = root / "data" / "session_health.jsonl"
        self.calls = []
        patcher = patch.multiple(
            sh.engine, _MU_EVENTS_ROOT=str(self.root), MU_EVENTS=str(self.root)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def diagnose(self, path):
        self.calls.append(path)
        return {"status": "clean", "session_id": "session-1", "open_calls": []}

    def refresh(self, files):
        with patch("builtins.print"):
            return sh.refresh(files, cache=self.cache, diagnose=self.diagnose)

    def test_a_row_names_the_machine_daemon_and_session(self):
        (row,) = self.refresh([str(self.log)])
        self.assertEqual(
            (row["machine"], row["daemon"], row["session_ref"]), ("m1", "d1", "d1:session-1")
        )

    def test_an_unchanged_log_is_not_read_again(self):
        self.refresh([str(self.log)])
        self.refresh([str(self.log)])
        self.assertEqual(len(self.calls), 1)

        self.log.write_text("{}\n{}\n")
        self.refresh([str(self.log)])
        self.assertEqual(len(self.calls), 2)

    def test_a_bad_cache_row_or_unreadable_log_does_not_stop_the_run(self):
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text('{"file": "x"}\n[1]\nnot json\n')
        other = self.log.with_name("session-2.jsonl")
        other.write_text("{}\n")

        def diagnose(path):
            if path == str(other):
                raise PermissionError(13, "denied")
            return self.diagnose(path)

        with patch("builtins.print"):
            rows = sh.refresh([str(self.log), str(other)], cache=self.cache, diagnose=diagnose)
        self.assertEqual([r["file"] for r in rows], [str(self.log)])

    def test_a_vanished_log_drops_out(self):
        self.refresh([str(self.log)])
        gone = str(self.log.with_name("session-2.jsonl"))
        rows = self.refresh([str(self.log), gone])
        self.assertEqual([r["file"] for r in rows], [str(self.log)])
        cached = [json.loads(line) for line in self.cache.read_text().splitlines()]
        self.assertEqual(len(cached), 1)


class Worklist(unittest.TestCase):
    def test_sections(self):
        rows = [
            _row("clean", "d0:session-1"),
            _row(
                "ragged",
                "d1:session-1",
                fork_event_id=3,
                open_calls=[{"call_id": "c1", "tool_name": "grep"}],
            ),
            _row("ragged", "d2:session-1", age_d=0.1, fork_event_id=5),
            _row("needs_predecessor", "d3:session-2", predecessor_session_id="session-1"),
            _row("gap", "d4:session-1", detail="unreadable line(s) [2]"),
        ]
        md = sh.worklist(rows, now=NOW)
        self.assertIn("5 session logs: ragged 2, clean 1", md)
        self.assertIn("| m1 | `d1:session-1` | grep | 3 | 10 | `mu recover d1:session-1` |", md)
        self.assertIn("--predecessor <daemon>:session-1", md)
        self.assertIn("| gap | unreadable line(s) [2] |", md)
        # a fresh ragged log is listed as possibly live, with no command
        live = md.split("## Possibly live")[1]
        self.assertIn("`d2:session-1`", live)
        self.assertNotIn("mu recover d2:session-1", md)


@unittest.skipUnless(hasattr(sh._EV, "diagnose_session"), "build-events-parser required")
class Binding(unittest.TestCase):
    def test_a_log_that_died_in_a_tool_call_is_ragged(self):
        def ev(i, payload):
            return {
                "id": i,
                "session_id": "session-1",
                "parent_event_ids": [],
                "timestamp_unix_ms": 1_700_000_000_000 + i,
                "actor": {"kind": "agent"},
                "payload": payload,
            }

        call = {
            "kind": "assistant_message_event",
            "message": {
                "content": [{"type": "tool_call", "id": "c1", "name": "grep", "arguments": {}}],
                "stop_reason": "tool_use",
                "usage": None,
            },
        }
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session-1.jsonl"
            events = [ev(1, {"kind": "user_message", "content": "q"}), ev(2, call)]
            path.write_text("".join(json.dumps(e) + "\n" for e in events))
            row = sh._diagnose(str(path))
        self.assertEqual(row["status"], "ragged")
        self.assertEqual(row["open_calls"], [{"call_id": "c1", "tool_name": "grep"}])
