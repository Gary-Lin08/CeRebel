"""Local accounts, library limits, and health report."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from motion_agent.app.accounts import login, register, user_from_token
from motion_agent.app.config import Settings
from motion_agent.app.health import health_report
from motion_agent.app.library import add_capture, captures_today, delete_capture, list_captures
from motion_agent.app.product_api import media_allowed, purge_session_dir


class AccountTests(unittest.TestCase):
    def test_register_login_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created = register(root, "coach@example.com", "swing-fix")
            self.assertTrue(created["token"])
            user = user_from_token(root, "Bearer " + created["token"])
            self.assertEqual(user["email"], "coach@example.com")
            again = login(root, "coach@example.com", "swing-fix")
            self.assertTrue(user_from_token(root, again["token"]))

    def test_library_and_daily_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            add_capture(root, user_id="u1", session_id="S1", filename="a.mp4")
            add_capture(root, user_id="u1", session_id="S2", filename="b.mp4")
            self.assertEqual(len(list_captures(root, "u1")), 2)
            self.assertEqual(captures_today(root, "u1"), 2)
            self.assertTrue(delete_capture(root, "u1", "S1"))
            self.assertEqual(len(list_captures(root, "u1")), 1)


class AccountDeleteTests(unittest.TestCase):
    def test_delete_account_removes_login_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            product = data / "product"
            session = data / "sessions" / "SID"
            session.mkdir(parents=True)
            (session / "clip.txt").write_text("gone", encoding="utf-8")
            created = register(product, "bye@example.com", "swing-fix")
            user = user_from_token(product, created["token"])
            add_capture(product, user_id=user["user_id"], session_id="SID", filename="a.mp4")
            settings = Settings(
                data_dir=data,
                host="127.0.0.1",
                port=1,
                backend="mock",
                prompt_password=None,
                max_upload_bytes=1024,
                invite_daily_limit=3,
            )
            from motion_agent.app.product_api import remove_account

            code, payload = remove_account(settings, user)
            self.assertEqual(code, 200)
            self.assertEqual(payload["deleted_captures"], 1)
            self.assertFalse(session.exists())
            self.assertIsNone(user_from_token(product, created["token"]))


class PrivacyTests(unittest.TestCase):
    def test_owner_gate_and_purge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            product = data / "product"
            session = data / "sessions" / "SID"
            session.mkdir(parents=True)
            (session / "clip.txt").write_text("private", encoding="utf-8")
            add_capture(product, user_id="owner", session_id="SID", filename="a.mp4")
            settings = Settings(
                data_dir=data,
                host="127.0.0.1",
                port=1,
                backend="mock",
                prompt_password=None,
                max_upload_bytes=1024,
                invite_daily_limit=3,
            )
            self.assertTrue(media_allowed(settings, {"user_id": "owner"}, "SID"))
            self.assertFalse(media_allowed(settings, None, "SID"))
            self.assertFalse(media_allowed(settings, {"user_id": "other"}, "SID"))
            self.assertTrue(media_allowed(settings, None, "DEMO"))
            purge_session_dir(settings, "SID")
            self.assertFalse(session.exists())
            outside = data / "not-a-session"
            outside.mkdir()
            purge_session_dir(settings, "../not-a-session")
            self.assertTrue(outside.exists())


class FinalizeCaptureTests(unittest.TestCase):
    def test_finalize_marks_golf_ready(self):
        from motion_agent.app.product_api import finalize_capture
        from motion_agent.sports.schema import PackSession

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            session = data / "sessions" / "GOLF1"
            (session / "ik").mkdir(parents=True)
            (session / "ik" / "opensim-motion.json").write_text("{}", encoding="utf-8")
            product = data / "product"
            add_capture(product, user_id="u1", session_id="GOLF1", filename="a.mp4")
            settings = Settings(
                data_dir=data,
                host="127.0.0.1",
                port=1,
                backend="mock",
                prompt_password=None,
                max_upload_bytes=1024,
                invite_daily_limit=3,
            )
            fake = PackSession(
                sport="golf",
                label="Golf",
                confidence=0.9,
                product_title="Your swing",
                product_subtitle="x",
                example_questions=[],
                default_prompt_mode="golf",
                available_modes=["golf"],
            )
            with mock.patch(
                "motion_agent.sports.registry.resolve_pack_session", return_value=fake
            ):
                result = finalize_capture(settings, "GOLF1")
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["sport"], "golf")
            rows = list_captures(product, "u1")
            self.assertEqual(rows[0]["status"], "ready")

    def test_finalize_marks_no_swing(self):
        from motion_agent.app.product_api import finalize_capture
        from motion_agent.sports.schema import PackSession

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            session = data / "sessions" / "GEN1"
            (session / "ik").mkdir(parents=True)
            (session / "ik" / "opensim-motion.json").write_text("{}", encoding="utf-8")
            product = data / "product"
            add_capture(product, user_id="u1", session_id="GEN1", filename="a.mp4")
            settings = Settings(
                data_dir=data,
                host="127.0.0.1",
                port=1,
                backend="mock",
                prompt_password=None,
                max_upload_bytes=1024,
                invite_daily_limit=3,
            )
            fake = PackSession(
                sport="general",
                label="Human motion",
                confidence=0.0,
                product_title="Your motion",
                product_subtitle="x",
                example_questions=[],
                default_prompt_mode="general",
                available_modes=["general"],
                detect_message="No confident golf swing",
            )
            with mock.patch(
                "motion_agent.sports.registry.resolve_pack_session", return_value=fake
            ):
                result = finalize_capture(settings, "GEN1")
            self.assertEqual(result["status"], "no_swing")
            self.assertFalse(result["consumer_ready"])


class HealthTests(unittest.TestCase):
    def test_missing_disk_is_not_ready(self):
        settings = Settings(
            data_dir=Path("/tmp/cerebel-missing-data-dir"),
            host="127.0.0.1",
            port=8092,
            backend="mock",
            prompt_password=None,
            max_upload_bytes=1024,
            invite_daily_limit=3,
        )
        code, payload = health_report(settings)
        self.assertEqual(code, 503)
        self.assertFalse(payload["disk"]["ok"])
        self.assertEqual(payload["process"], "up")
        self.assertTrue(payload["model"]["ok"])


if __name__ == "__main__":
    unittest.main()
