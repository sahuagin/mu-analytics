import json
import os
import tempfile
import unittest

import fixtures

import pointwise_driver as driver
import pointwise_judge as pointwise


class TestPointwiseDriver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def write_allowlist(self, value):
        path = os.path.join(self.tmp.name, "allowlist.json")
        with open(path, "w") as f:
            json.dump(value, f)
        return path

    def test_allowlist_is_exact_nonempty_and_unique(self):
        path = self.write_allowlist(
            {
                "sessions": [
                    {"fleet": "mu", "session": "daemon:s1"},
                    {"fleet": "cc", "session": "cc-session-id"},
                ]
            }
        )
        sessions = driver.load_allowlist(path)
        self.assertEqual([s.ref for s in sessions], ["mu:daemon:s1", "cc:cc-session-id"])

        for bad in (
            {"sessions": []},
            {"sessions": [{"fleet": "bad", "session": "x"}]},
            {"sessions": [{"fleet": "mu", "session": ""}]},
            {
                "sessions": [
                    {"fleet": "mu", "session": "x"},
                    {"fleet": "mu", "session": "x"},
                ]
            },
            {"sessions": [{"fleet": "mu", "session": "x", "extra": True}]},
            {"sessions": [], "other": True},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    driver.load_allowlist(self.write_allowlist(bad))

    def test_driver_pins_target_continues_after_error_and_emits_aggregates(self):
        sessions = [
            driver.AllowedSession("mu", "txdaemon:s1"),
            driver.AllowedSession("cc", "txcc-0000-1111"),
        ]
        calls = []

        def processor(con, fleet, session, cls, provider, store, *args, **kwargs):
            calls.append((fleet, session, cls, provider, kwargs["pointwise_profile"]))
            self.assertEqual(kwargs["target_resolver"]("judge", provider), "flashnext/model")
            if fleet == "cc":
                raise RuntimeError("private provider-derived diagnostic")
            return {
                "chunks": 2,
                "units_total": 11,
                "accepted": 5,
                "quarantined": 0,
                "remaining": 6,
                "target": "flashnext/model",
            }

        resolved = []

        def resolver(role, provider):
            resolved.append((role, provider))
            return "flashnext/model"

        store = pointwise.ResultStore(os.path.join(self.tmp.name, "result.sqlite"))
        try:
            rows, failures = driver.run_allowlist(
                object(),
                sessions,
                "flashnext",
                store,
                65536,
                1200,
                900,
                1,
                processor=processor,
                target_resolver=resolver,
            )
        finally:
            store.close()

        self.assertEqual(resolved, [("judge", "flashnext")])
        self.assertEqual(failures, 1)
        self.assertEqual([r["status"] for r in rows], ["ok", "error"])
        self.assertEqual(
            rows[1],
            {
                "session_ref": "cc:txcc-0000-1111",
                "status": "error",
                "error_type": "RuntimeError",
            },
        )
        self.assertNotIn("private", json.dumps(rows))
        self.assertTrue(all(c[-1] is True for c in calls))

    def test_plan_uses_existing_projection_and_returns_no_content(self):
        con = fixtures.transcript_connection(self.tmp.name)
        rows, failures = driver.plan_allowlist(
            con,
            [
                driver.AllowedSession("mu", "txdaemon:s1"),
                driver.AllowedSession("mu", "missing:s1"),
            ],
            90,
            20,
        )
        self.assertEqual(failures, 1)
        self.assertEqual(rows[0]["status"], "planned")
        self.assertGreater(rows[0]["chunks"], 1)
        self.assertEqual(
            rows[0]["units_total"],
            rows[0]["chunks"] * len(pointwise.POINTWISE_CLASSES)
            + len(pointwise.FINAL_CHUNK_CLASSES),
        )
        self.assertEqual(rows[1]["error_type"], "RuntimeError")
        rendered = json.dumps(rows)
        self.assertNotIn("please fix", rendered)
        self.assertNotIn("tests passed", rendered)


if __name__ == "__main__":
    unittest.main()
