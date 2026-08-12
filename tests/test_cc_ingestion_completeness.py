"""bead mu-tijj: the cc transcript sweep must be COMPLETE and RECONCILED.

A fixed depth-1 glob once ingested only ~54% of on-disk transcripts (375/691)
— silently. discover_transcripts() is the seam that guards against a repeat:
the recursive glob must find transcripts at ANY nesting depth (the canonical
projects/<slug>/<uuid>.jsonl layout, experiments/ subtrees), overlapping roots
must dedup, history.jsonl must be dropped by name, and the reconciliation
summary must account for every raw glob hit — so a future discovery gap shows
up as a printed delta, not a silent loss. main()'s emit accounting must then
bucket every candidate (emitted / skipped / denied) with no unexplained delta.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cc_telemetry as cct  # noqa: E402


def _transcript_records():
    """A minimal REAL transcript: one user turn + one assistant end_turn."""
    return [
        {
            "type": "user",
            "timestamp": "2026-06-15T10:00:00.000Z",
            "message": {"role": "user", "content": "hello"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-06-15T10:00:01.000Z",
            "message": {
                "id": "msg_1",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        },
    ]


class TestDiscoverTranscripts(unittest.TestCase):
    """The pure discovery seam: nested layouts, dedup, exclusion, reconciliation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.archive = os.path.join(self._tmp.name, "archive")
        # depth 1: a transcript directly under the root (old glob's only reach
        # was one fixed layer; both shallow and deep must be found now).
        self.depth1 = self._touch("sess-depth1.jsonl")
        # canonical depth-2+ layout: <machine>/projects/<slug>/<uuid>.jsonl
        self.uuid1 = self._touch("machine1", "projects", "slug-a", "uuid-1.jsonl")
        self.uuid2 = self._touch("machine1", "projects", "slug-a", "uuid-2.jsonl")
        self.uuid3 = self._touch("machine2", "projects", "slug-b", "uuid-3.jsonl")
        # experiments/ subtree (goal-run transcripts)
        self.exp1 = self._touch("machine1", "experiments", "goal-x", "run-1.jsonl")
        # per-machine prompt-history file: NOT a transcript, must be dropped
        self._touch("machine1", "history.jsonl")
        # non-jsonl noise: must never match the glob
        self._touch("machine1", "notes.txt")
        self._touch("machine1", "projects", "slug-a", "README.md")
        # overlapping roots: machine1 is also listed as its own root, so every
        # jsonl under it is matched twice and must dedup to one path.
        self.roots = [self.archive, os.path.join(self.archive, "machine1")]
        self.expected = sorted([self.depth1, self.uuid1, self.uuid2, self.uuid3, self.exp1])

    def _touch(self, *rel):
        path = os.path.join(self.archive, *rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write('{"type": "summary"}\n')
        return path

    def _patterns(self):
        return [os.path.join(r, "**", "*.jsonl") for r in self.roots]

    def test_finds_exactly_the_expected_set(self):
        files, _recon = cct.discover_transcripts(self._patterns())
        self.assertEqual(files, self.expected)  # sorted, deduped, history-free

    def test_reconciliation_numbers_add_up(self):
        _files, recon = cct.discover_transcripts(self._patterns())
        # raw hits: archive root sees 6 jsonl (5 transcripts + history);
        # the machine1 root re-matches its 4 (uuid-1, uuid-2, run-1, history).
        self.assertEqual(
            recon,
            {
                "files_on_disk_matching_glob": 10,
                "deduped": 4,
                "dropped_non_transcript": 1,
                "candidates": 5,
            },
        )
        self.assertEqual(
            recon["candidates"],
            recon["files_on_disk_matching_glob"]
            - recon["deduped"]
            - recon["dropped_non_transcript"],
        )

    def test_single_root_no_overlap_deduped_zero(self):
        files, recon = cct.discover_transcripts(
            [os.path.join(self.archive, "machine2", "**", "*.jsonl")]
        )
        self.assertEqual(files, [self.uuid3])
        self.assertEqual(recon["deduped"], 0)
        self.assertEqual(recon["dropped_non_transcript"], 0)
        self.assertEqual(recon["candidates"], 1)


class TestMainReconciliation(unittest.TestCase):
    """main() buckets every discovered candidate: emitted + skipped (+ denied)
    == candidates, printed for the operator with no unexplained delta."""

    def setUp(self):
        # module-level visibility counters accumulate; reset for isolation.
        cct._UNMAPPED_BLOCKS.clear()
        cct._SKIPPED_TYPES.clear()
        cct._PARSE["typed"] = cct._PARSE["fallback"] = 0

    def test_emit_accounting_covers_all_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "archive")
            slug = os.path.join(root, "machine1", "projects", "slug-a")
            os.makedirs(slug, exist_ok=True)
            with open(os.path.join(slug, "uuid-9.jsonl"), "w") as f:
                for o in _transcript_records():
                    f.write(json.dumps(o) + "\n")
            # metadata-only jsonl: a candidate, but no assistant turns -> skipped
            with open(os.path.join(root, "machine1", "meta-only.jsonl"), "w") as f:
                f.write('{"type": "summary", "summary": "not a transcript"}\n')
            # history.jsonl: dropped at discovery, never a candidate
            with open(os.path.join(root, "machine1", "history.jsonl"), "w") as f:
                f.write('{"display": "prompt history"}\n')
            out_dir = os.path.join(d, "out")
            argv = ["cc_telemetry.py", os.path.join(root, "**", "*.jsonl"), out_dir]
            buf = io.StringIO()
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
                cct.main()
            printed = buf.getvalue()
            # discovery side: 3 on-disk, history dropped, 2 candidates
            self.assertIn("discovered 3 on-disk jsonl -> 2 candidate transcript(s)", printed)
            self.assertIn("0 duplicate(s) deduped, 1 non-transcript dropped by name", printed)
            # ingest side: candidates fully explained -> emitted 1 + skipped 1
            self.assertIn("emitted 1 session(s)", printed)
            self.assertIn("1 skipped (non-transcript)", printed)
            self.assertNotIn("WARNING", printed)
            # the emitted session landed under <out>/claude-code/<sid>.jsonl
            self.assertTrue(os.path.exists(os.path.join(out_dir, "claude-code", "uuid-9.jsonl")))


if __name__ == "__main__":
    unittest.main()
