"""What the waiting screen gets while a swing is being analyzed."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from motion_agent.app import chat_server
from motion_agent.app.accounts import register
from motion_agent.app.config import Settings
from motion_agent.app.library import add_capture


class ProcessingPayloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = Settings(
            data_dir=root, host="127.0.0.1", port=1, backend="mock",
            prompt_password=None, max_upload_bytes=1024, invite_daily_limit=5,
        )
        created = register(self.settings.product_dir, "waiting@example.test", secrets.token_urlsafe(16), accepted_terms=True)
        self.token = created["token"]
        add_capture(self.settings.product_dir, user_id=created["user"]["user_id"], session_id="S-WAIT", filename="a.mp4", status="gemx")
        progress = root / "sessions" / "S-WAIT" / "upload_progress.json"
        progress.parent.mkdir(parents=True)
        progress.write_text(json.dumps({"session_id": "S-WAIT", "stage": "gemx", "percent": 46, "message": "Rebuilding body"}))
        self._patches = [
            mock.patch.object(chat_server, "SETTINGS", self.settings),
            mock.patch.object(chat_server, "DATA_DIR", root),
        ]
        for p in self._patches:
            p.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
        self.server.operator = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _session(self) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}/api/session?session_id=S-WAIT",
            headers={"Authorization": "Bearer " + self.token},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def test_step_and_progress_without_email(self):
        with mock.patch.dict(os.environ, {"CEREBEL_SMTP_HOST": "", "CEREBEL_MAIL_FROM": ""}):
            data = self._session()
        self.assertTrue(data["processing"])
        self.assertEqual(data["stage"], "gemx")
        self.assertEqual(data["percent"], 46)
        self.assertFalse(data["email_when_ready"])
        self.assertIn("updates on its own", data["product_subtitle"])

    def test_email_note_when_mail_is_configured(self):
        with mock.patch.dict(os.environ, {"CEREBEL_SMTP_HOST": "smtp.example.test", "CEREBEL_MAIL_FROM": "a@example.test"}):
            data = self._session()
        self.assertTrue(data["email_when_ready"])
        self.assertIn("email you", data["product_subtitle"])


if __name__ == "__main__":
    unittest.main()
