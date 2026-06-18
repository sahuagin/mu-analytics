"""DS2: audit_sweep (moved from mu_audit_sweep). The sweep() subprocess path needs
the `mu` binary + real logs, so this tests the parse logic that can silently drift:
the FINDING regex and first_ts. The canonical colon session_ref is asserted via the
format the sweep builds."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audit_sweep as a  # noqa: E402


class TestAuditParse(unittest.TestCase):
    def test_finding_regex(self):
        out = (
            "scanning...\n"
            "[High] repeated_identical_tool_call @event 452: tool `write` called 3x\n"
            "[Low] some_other_invariant @event 7: minor note with\ttab\n"
            "done\n"
        )
        found = a.FINDING.findall(out)
        self.assertEqual(len(found), 2)
        sev, inv, ev, detail = found[0]
        self.assertEqual((sev, inv, ev), ("High", "repeated_identical_tool_call", "452"))
        # detail tab-stripping (TSV safety) happens in sweep(); verify the swap
        self.assertEqual(found[1][3].replace("\t", " "), "minor note with tab")

    def test_first_ts_reads_leading_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "session-1.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps({"timestamp_unix_ms": 1781000000000, "payload": {}}) + "\n")
            ts = a.first_ts(p)
        self.assertTrue(ts.startswith("2026-"))  # ISO, UTC
        self.assertEqual(a.first_ts("/nonexistent/x.jsonl"), "")

    def test_session_ref_colon_form(self):
        # the canonical fleet:daemon:session_id form the dashboard joins on
        f = "/home/x/.local/share/mu/events/1a7812f064510d91/session-1.jsonl"
        daemon, sid = f.split("/")[-2], os.path.basename(f)[:-6]
        self.assertEqual(f"mu:{daemon}:{sid}", "mu:1a7812f064510d91:session-1")

    def test_audit_workers_bounds_and_override(self):
        old = os.environ.pop("MU_ANALYTICS_AUDIT_WORKERS", None)
        try:
            self.assertEqual(a.audit_workers(0), 1)
            self.assertGreaterEqual(a.audit_workers(100), 1)
            self.assertLessEqual(a.audit_workers(100), 8)
            os.environ["MU_ANALYTICS_AUDIT_WORKERS"] = "2"
            self.assertEqual(a.audit_workers(100), 2)
            os.environ["MU_ANALYTICS_AUDIT_WORKERS"] = "not-int"
            self.assertLessEqual(a.audit_workers(100), 8)
        finally:
            if old is None:
                os.environ.pop("MU_ANALYTICS_AUDIT_WORKERS", None)
            else:
                os.environ["MU_ANALYTICS_AUDIT_WORKERS"] = old


if __name__ == "__main__":
    unittest.main()
