import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fixtures  # noqa: E402

import marks_store  # noqa: E402


class TestMarksStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_db, self._orig_inbox = marks_store.MARKS_DB, marks_store.INBOX
        marks_store.MARKS_DB = os.path.join(self.tmp.name, "marks.sqlite")
        marks_store.INBOX = os.path.join(self.tmp.name, "inbox")

    def tearDown(self):
        marks_store.MARKS_DB, marks_store.INBOX = self._orig_db, self._orig_inbox
        self.tmp.cleanup()

    def test_coerce_rating(self):
        self.assertEqual(marks_store._coerce_rating("4"), 4)
        self.assertEqual(marks_store._coerce_rating("0"), 1)  # clamp low
        self.assertEqual(marks_store._coerce_rating("9"), 5)  # clamp high
        self.assertEqual(marks_store._coerce_rating("abc"), 3)  # non-numeric -> mid
        self.assertEqual(marks_store._coerce_rating(None), 3)
        self.assertEqual(marks_store._coerce_rating(4.0), 4)

    def test_add_and_read_union(self):
        con = fixtures.fixture_connection(self.tmp.name)  # 1 operator_mark (rating "4")
        marks_store.add_mark("task-x", 5, "great", created_at_unix_ms=1_700_000_500_000)
        marks = marks_store.read_marks(con)
        self.assertEqual(len(marks), 2)  # event-log + dashboard
        self.assertEqual(sorted(m["source"] for m in marks), ["dashboard", "mu_event"])
        dash = next(m for m in marks if m["source"] == "dashboard")
        self.assertEqual(dash["rating"], 5)
        self.assertEqual(dash["note"], "great")

    def test_ingest_inbox(self):
        os.makedirs(marks_store.INBOX, exist_ok=True)
        path = os.path.join(marks_store.INBOX, "export.jsonl")
        with open(path, "w") as f:
            f.write(
                json.dumps(
                    {
                        "task_id": "t1",
                        "rating": 3,
                        "note": "n",
                        "created_at_unix_ms": 1_700_000_600_000,
                    }
                )
                + "\n"
            )
        self.assertEqual(marks_store.ingest_inbox(), 1)
        self.assertTrue(os.path.exists(path + ".ingested"))  # consumed file archived
        rows = (
            sqlite3.connect(marks_store.MARKS_DB)
            .execute("SELECT task_id, rating FROM marks")
            .fetchall()
        )
        self.assertIn(("t1", 3), rows)

    def test_ingest_missing_inbox_is_noop(self):
        self.assertEqual(marks_store.ingest_inbox("/nonexistent/inbox/path"), 0)


if __name__ == "__main__":
    unittest.main()
