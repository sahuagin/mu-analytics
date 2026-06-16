"""DS1: admin_usage (Anthropic Admin API reconciliation) port. Tests the pure
pricing/window/family functions and the no-key degradation path — all without
`requests` or the live API (the `admin` extra and the network call are not exercised
in CI; the key check runs before the requests import so no-key degrades cleanly)."""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin_usage as au  # noqa: E402


class TestPricing(unittest.TestCase):
    def test_family(self):
        self.assertEqual(au._family("claude-opus-4-7"), "opus")
        self.assertEqual(au._family("claude-sonnet-4-6"), "sonnet")
        self.assertEqual(au._family("claude-haiku-4-5"), "haiku")
        self.assertEqual(au._family("something-else"), "haiku")  # default
        self.assertEqual(au._family(""), "haiku")

    def test_list_price_usd(self):
        # opus: 1M uncached_input @ $5 + 1M output @ $25 + 1M cache_read @ $0.50
        #       + 1M cw_5m @ $6.25 + 1M cw_1h @ $10  => $46.75
        rec = {
            "model": "claude-opus-4-7",
            "uncached_input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 1_000_000,
                "ephemeral_1h_input_tokens": 1_000_000,
            },
        }
        self.assertAlmostEqual(au.list_price_usd(rec), 46.75, places=6)

    def test_list_price_missing_fields_default_zero(self):
        self.assertEqual(au.list_price_usd({"model": "claude-haiku-4-5"}), 0.0)


class TestWindow(unittest.TestCase):
    def test_utc_window_trailing_days(self):
        # 7-day window ending 2026-06-15 -> [2026-06-09, 2026-06-16) (today's partial incl.)
        start, end = au._utc_window(7, today=date(2026, 6, 15))
        self.assertEqual(start, "2026-06-09T00:00:00Z")
        self.assertEqual(end, "2026-06-16T00:00:00Z")


class TestAuthAndDegradation(unittest.TestCase):
    def test_admin_key_env_first(self):
        os.environ["ANTHROPIC_ADMIN_KEY"] = "sk-ant-admin-test"
        try:
            self.assertEqual(au.admin_key(), "sk-ant-admin-test")
        finally:
            del os.environ["ANTHROPIC_ADMIN_KEY"]

    def test_admin_key_absent_returns_none(self):
        os.environ.pop("ANTHROPIC_ADMIN_KEY", None)
        os.environ["ANTHROPIC_ADMIN_KEY_FILE"] = "/nonexistent/admin/key/file"
        try:
            self.assertIsNone(au.admin_key())
        finally:
            del os.environ["ANTHROPIC_ADMIN_KEY_FILE"]

    def test_fetch_reconciliation_degrades_without_key(self):
        # no key + no requests needed: _get checks the key before importing requests,
        # so fetch_reconciliation returns a safe {"ok": False} note rather than raising.
        os.environ.pop("ANTHROPIC_ADMIN_KEY", None)
        os.environ["ANTHROPIC_ADMIN_KEY_FILE"] = "/nonexistent/admin/key/file"
        try:
            out = au.fetch_reconciliation(days=7, today=date(2026, 6, 15))
        finally:
            del os.environ["ANTHROPIC_ADMIN_KEY_FILE"]
        self.assertFalse(out["ok"])
        self.assertIn("no admin key", out["note"])


if __name__ == "__main__":
    unittest.main()
