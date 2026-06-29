"""incidents.load — parse a notes dir of incident reports into dated timeline
events. Hermetic: writes temp .md files and asserts date/title/polarity/slug,
session_ref extraction + mu slash->colon normalization, prefix-based skipping,
and graceful handling of a missing dir."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import incidents  # noqa: E402


class TestIncidents(unittest.TestCase):
    def _dir(self, files):
        d = tempfile.mkdtemp()
        for name, body in files.items():
            with open(os.path.join(d, name), "w") as fh:
                fh.write(body)
        return d

    def test_parse_issue_and_positive(self):
        d = self._dir(
            {
                "incident-2026-06-19-ollama-gpu-thrash.md": (
                    "# Ollama GPU thrash — 2026-06-19 postmortem\n\n"
                    "session: cc:cde1e858-4e1d-4379-9856-5bbe282fa731\n"
                ),
                "checkpoint-2026-06-07-thesis-validation.md": (
                    "# Autonomous thesis — validation\n\nref mu:7517502faa5f7ed2/session-1 here\n"
                ),
            }
        )
        rows = incidents.load(d)
        self.assertEqual(len(rows), 2)
        # date-sorted: checkpoint (06-07) before incident (06-19)
        cp, inc = rows
        self.assertEqual(cp["date"], "2026-06-07")
        self.assertEqual(cp["polarity"], "positive")
        self.assertEqual(cp["kind"], "checkpoint")
        # mu slash-form normalized to the colon-form ev/marks use
        self.assertEqual(cp["session_refs"], ["mu:7517502faa5f7ed2:session-1"])
        self.assertEqual(inc["date"], "2026-06-19")
        self.assertEqual(inc["polarity"], "issue")
        self.assertEqual(inc["title"], "Ollama GPU thrash — 2026-06-19 postmortem")
        self.assertEqual(inc["session_refs"], ["cc:cde1e858-4e1d-4379-9856-5bbe282fa731"])
        self.assertEqual(inc["slug"], "ollama-gpu-thrash")

    def test_unmapped_prefix_and_undated_skipped(self):
        d = self._dir(
            {
                "billing-strategy-2026-05-21.md": "# Billing\n",  # unmapped prefix
                "incident-no-date-here.md": "# No date\n",  # no YYYY-MM-DD
                "incident-2026-06-20-real.md": "# Real incident\n",
            }
        )
        rows = incidents.load(d)
        self.assertEqual([r["file"] for r in rows], ["incident-2026-06-20-real.md"])

    def test_missing_dir_is_empty(self):
        self.assertEqual(incidents.load("/no/such/notes/dir/xyz"), [])

    def test_dedupes_session_refs_in_order(self):
        d = self._dir(
            {
                "incident-2026-06-12-thread.md": (
                    "# Thread\n\nfirst cc:511ff8ec-078a-4726-abe1-eec617c10619\n"
                    "again cc:511ff8ec-078a-4726-abe1-eec617c10619\n"
                    "other cc:5b23eb4d-05ba-4bd2-8797-8c8caf5cc876\n"
                )
            }
        )
        (row,) = incidents.load(d)
        self.assertEqual(
            row["session_refs"],
            [
                "cc:511ff8ec-078a-4726-abe1-eec617c10619",
                "cc:5b23eb4d-05ba-4bd2-8797-8c8caf5cc876",
            ],
        )


if __name__ == "__main__":
    unittest.main()
