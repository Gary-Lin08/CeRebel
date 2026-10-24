"""Local accounts, library limits, and health report."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from motion_agent.app.accounts import login, register, user_from_token
from motion_agent.app.config import Settings
from motion_agent.app.health import health_report
from motion_agent.app.library import add_capture, captures_today, delete_capture, list_captures
from motion_agent.app.product_api import default_visible_session_id, media_allowed, purge_session_dir


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
            self.assertFalse(media_allowed(settings, None, "DEMO"))
            demo = data / "sessions" / "DEMO" / "ik"
            demo.mkdir(parents=True)
            (demo / "opensim-motion.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CEREBEL_DEMO_SESSION_ID": "DEMO"}):
                self.assertTrue(media_allowed(settings, None, "DEMO"))
                self.assertEqual(default_visible_session_id(settings, data, None), "DEMO")
                self.assertFalse(media_allowed(settings, None, "SID"))
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


class DefaultSessionScopeTests(unittest.TestCase):
    """Endpoints without a session id must not fall back to another account's upload."""

    def _settings(self, data: Path) -> Settings:
        return Settings(
            data_dir=data,
            host="127.0.0.1",
            port=1,
            backend="mock",
            prompt_password=None,
            max_upload_bytes=1024,
            invite_daily_limit=3,
        )

    def _ready(self, data: Path, sid: str, mtime: float) -> None:
        motion = data / "sessions" / sid / "ik" / "opensim-motion.json"
        motion.parent.mkdir(parents=True)
        motion.write_text("{}", encoding="utf-8")
        os.utime(motion, (mtime, mtime))

    def test_default_skips_other_accounts(self):
        from motion_agent.app.product_api import default_visible_session_id

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            self._ready(data, "DEMO", 1_000)
            self._ready(data, "PRIVATE", 2_000)  # newer, but owned by someone
            add_capture(data / "product", user_id="owner", session_id="PRIVATE", filename="a.mp4")
            settings = self._settings(data)
            self.assertIsNone(default_visible_session_id(settings, data, None))
            with mock.patch.dict(os.environ, {"CEREBEL_DEMO_SESSION_ID": "DEMO"}):
                self.assertEqual(default_visible_session_id(settings, data, None), "DEMO")
                self.assertEqual(default_visible_session_id(settings, data, {"user_id": "owner"}), "PRIVATE")
                self.assertEqual(default_visible_session_id(settings, data, {"user_id": "other"}), "DEMO")

    def test_no_demo_means_no_default(self):
        from motion_agent.app.product_api import default_visible_session_id

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            self._ready(data, "PRIVATE", 2_000)
            add_capture(data / "product", user_id="owner", session_id="PRIVATE", filename="a.mp4")
            self.assertIsNone(default_visible_session_id(self._settings(data), data, None))

    def test_progress_scope_for_anonymous(self):
        from motion_agent.app.product_api import user_may_view_session

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            self._ready(data, "DEMO", 1_000)
            add_capture(data / "product", user_id="owner", session_id="PRIVATE", filename="a.mp4")
            settings = self._settings(data)
            self.assertFalse(user_may_view_session(settings, None, "PRIVATE"))
            self.assertFalse(user_may_view_session(settings, None, "DEMO"))
            with mock.patch.dict(os.environ, {"CEREBEL_DEMO_SESSION_ID": "DEMO"}):
                self.assertTrue(user_may_view_session(settings, None, "DEMO"))
            self.assertTrue(user_may_view_session(settings, {"user_id": "owner"}, "PRIVATE"))
            self.assertFalse(user_may_view_session(settings, {"user_id": "other"}, "PRIVATE"))


class TokenExpiryTests(unittest.TestCase):
    def test_expired_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created = register(root, "late@example.com", "swing-fix")
            self.assertTrue(user_from_token(root, created["token"]))
            tokens_path = root / "tokens.json"
            rows = json.loads(tokens_path.read_text(encoding="utf-8"))
            self.assertIn("expires_at", rows[created["token"]])
            rows[created["token"]]["expires_at"] = "2000-01-01T00:00:00+00:00"
            tokens_path.write_text(json.dumps(rows), encoding="utf-8")
            self.assertIsNone(user_from_token(root, created["token"]))

    def test_legacy_token_uses_created_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now(timezone.utc).isoformat()
            legacy = {
                "old": {"user_id": "u", "email": "a@b.co", "created_at": "2000-01-01T00:00:00+00:00"},
                "fresh": {"user_id": "u", "email": "a@b.co", "created_at": now},
            }
            (root / "tokens.json").write_text(json.dumps(legacy), encoding="utf-8")
            self.assertIsNone(user_from_token(root, "old"))
            self.assertEqual(user_from_token(root, "fresh")["user_id"], "u")

    def test_wrong_password_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            register(root, "pw@example.com", "swing-fix")
            with self.assertRaises(ValueError):
                login(root, "pw@example.com", "wrong-pass")
            with self.assertRaises(ValueError):
                login(root, "nobody@example.com", "swing-fix")


if __name__ == "__main__":
    unittest.main()


class TestPublicProgress(unittest.TestCase):
    def test_raw_exception_is_hidden(self):
        from motion_agent.app.product_api import PIPELINE_FAILED_MESSAGE, public_progress

        raw = "Lookup failed for Cls 'Gemx' from the 'cerebel-gemx' app"
        out = public_progress({"stage": "error", "message": raw, "error": raw})
        self.assertEqual(out["message"], PIPELINE_FAILED_MESSAGE)
        self.assertNotIn("error", out)

    def test_written_message_is_kept(self):
        from motion_agent.app.product_api import public_progress

        out = public_progress({"stage": "error", "message": "File is too small", "error": "too small"})
        self.assertEqual(out["message"], "File is too small")
        self.assertEqual(public_progress({"stage": "gemx", "message": "x", "error": "x"})["error"], "x")
