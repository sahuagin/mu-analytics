"""DS2: panels.delegations — the Delegations page wired off ev's worker_*/mailbox_*
events (was a stub). Hermetic: a session that spawns two workers (one exits clean,
one fails) + mailbox traffic; asserts the best-effort spawn↔terminal pairing (by
order within the session), outcome aggregation, and mailbox counts."""

import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402
import panels  # noqa: E402

_T0 = int(datetime(2026, 6, 15, 13, 0, 0, tzinfo=UTC).timestamp() * 1000)


def _ev(i, kind, payload):
    return {
        "id": i,
        "session_id": "session-1",
        "timestamp_unix_ms": _T0 + i * 1000,
        "actor": {},
        "payload": {"kind": kind, **payload},
    }


class TestDelegations(unittest.TestCase):
    def test_pairs_workers_and_aggregates_mailbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = os.path.join(tmp, "events", "d1", "session-1.jsonl")
            os.makedirs(os.path.dirname(ev))
            events = [
                _ev(
                    1,
                    "worker_spawned",
                    {
                        "pot_name": "p1",
                        "model": "claude-opus-4-8",
                        "pid": 1,
                        "prompt_summary": "first",
                    },
                ),
                _ev(2, "worker_exited", {"exit_code": 0, "elapsed_ms": 1000}),
                _ev(
                    3,
                    "worker_spawned",
                    {"pot_name": "p2", "model": "gpt-5.5", "pid": 2, "prompt_summary": "second"},
                ),
                _ev(4, "worker_failed", {"reason": "boom"}),
                _ev(
                    5,
                    "mailbox_message_posted",
                    {"seq": 1, "message_kind": "task", "subject": "do it"},
                ),
                _ev(6, "mailbox_message_consumed", {"seq": 1}),
            ]
            with open(ev, "w") as f:
                for e in events:
                    f.write(json.dumps(e) + "\n")
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
            d = panels.delegations(con)

        self.assertEqual(d["orchestrators"], 1)
        by_pot = {w["pot"]: w for w in d["workers"]}
        # spawn[0] (p1) pairs with term[0] (exited 0); spawn[1] (p2) with term[1] (failed)
        self.assertEqual(by_pot["p1"]["outcome"], "exited")
        self.assertEqual(by_pot["p1"]["detail"], "exit 0")
        self.assertEqual(by_pot["p1"]["model"], "claude-opus-4-8")
        self.assertEqual(by_pot["p2"]["outcome"], "failed")
        self.assertEqual(by_pot["p2"]["detail"], "boom")
        outcomes = {o["outcome"]: o["n"] for o in d["by_outcome"]}
        self.assertEqual(outcomes, {"exited": 1, "failed": 1})
        self.assertEqual(d["mailbox"]["posted"], 1)
        self.assertEqual(d["mailbox"]["consumed"], 1)
        self.assertEqual(d["mailbox"]["by_kind"], [{"kind": "task", "n": 1}])

    def test_unmatched_old_worker_is_unknown_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = os.path.join(tmp, "events", "d1", "session-1.jsonl")
            os.makedirs(os.path.dirname(ev))
            with open(ev, "w") as f:
                f.write(
                    json.dumps(
                        _ev(
                            1,
                            "worker_spawned",
                            {
                                "pot_name": "p1",
                                "model": "claude-opus-4-8",
                                "pid": 1,
                                "prompt_summary": "orphaned",
                            },
                        )
                    )
                    + "\n"
                )
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
            d = panels.delegations(con, now_ms=_T0 + 7 * 60 * 60 * 1000)

        self.assertEqual(d["workers"][0]["outcome"], "unknown-stale")
        self.assertEqual(d["workers"][0]["detail"], "no terminal event recorded")
        outcomes = {o["outcome"]: o["n"] for o in d["by_outcome"]}
        self.assertEqual(outcomes, {"unknown-stale": 1})
