"""Optional checks against a real server. Skipped unless CEREBEL_TEST_BASE_URL is set.

    CEREBEL_TEST_BASE_URL=http://127.0.0.1:8092 python -m unittest tests.test_live_server

Creates a throwaway account (deleted at the end). With CEREBEL_TEST_UPLOAD=/path/to/swing.mp4 it
also uploads that clip and waits for the report: this uses one analysis and needs the GPU worker.
Never point it at production with a real person's account.
"""

from __future__ import annotations

import os
import secrets
import unittest

from cerebel import CerebelClient, ConsentRequired, main_fix

BASE = os.environ.get("CEREBEL_TEST_BASE_URL", "").strip()
UPLOAD = os.environ.get("CEREBEL_TEST_UPLOAD", "").strip()


@unittest.skipUnless(BASE, "set CEREBEL_TEST_BASE_URL to run against a real server")
class LiveServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = CerebelClient(BASE)
        cls.email = f"sdk-test-{secrets.token_hex(4)}@example.com"
        cls.client.register(cls.email, secrets.token_urlsafe(12), accept_terms=True)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.client.delete_account()
        except Exception:  # noqa: BLE001
            pass

    def test_health_and_catalog(self) -> None:
        health = self.client.health()
        self.assertEqual(health["process"], "up")
        self.assertIn("state", health["gpu"])
        live = [s["id"] for s in self.client.sports()["sports"] if s["status"] == "live"]
        self.assertIn("golf", live)

    def test_account(self) -> None:
        me = self.client.me()
        self.assertEqual(me["user"]["email"], self.email)
        self.assertIn("remaining", me["quota"])
        self.assertTrue(me["video_consent"]["text"])

    def test_demo_report_and_coach(self) -> None:
        demo = self.client.session(sample=True)
        if demo.get("empty"):
            self.skipTest("this server has no CEREBEL_DEMO_SESSION_ID")
        sid = demo["session_id"]
        report = self.client.sport_analysis(sid)
        self.assertIsInstance(report.get("findings"), list)
        if report["findings"]:
            self.assertTrue(main_fix(report)["headline"])
        self.assertIn("briefs", self.client.coach_briefs(sid))
        self.assertGreaterEqual(self.client.golf_phases(sid).get("swing_count", 0), 0)
        events = list(self.client.ask_stream("What's the biggest thing I should fix in this swing?", sid))
        self.assertEqual(events[-1]["type"], "done")
        self.assertTrue(events[-1].get("answer"))

    @unittest.skipUnless(UPLOAD, "set CEREBEL_TEST_UPLOAD to a swing clip to test the full upload")
    def test_upload_journey(self) -> None:
        with self.assertRaises(ConsentRequired):
            self.client.upload(UPLOAD)
        self.client.accept_video_consent()
        result = self.client.analyze(UPLOAD, on_progress=lambda p: print("  progress:", p.get("stage"), p.get("percent")))
        self.assertEqual(result["progress"]["stage"], "ready")


if __name__ == "__main__":
    unittest.main()
