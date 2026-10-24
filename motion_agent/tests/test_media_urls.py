"""Signed /media URLs: <video> and loaders can read the owner's private capture without headers."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from motion_agent.app import chat_server, media_urls
from motion_agent.app.accounts import register
from motion_agent.app.config import Settings
from motion_agent.app.library import add_capture, delete_capture


def _settings(data: Path) -> Settings:
    return Settings(
        data_dir=data,
        host="127.0.0.1",
        port=0,
        backend="mock",
        prompt_password=None,
        max_upload_bytes=1024,
        invite_daily_limit=3,
    )


class TokenTests(unittest.TestCase):
    def test_token_roundtrip_expiry_and_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp)
            tok = media_urls.make_token(product, "SID", now=1000)
            self.assertTrue(media_urls.token_valid(product, "SID", tok, now=1001))
            self.assertFalse(media_urls.token_valid(product, "OTHER", tok, now=1001))
            self.assertFalse(media_urls.token_valid(product, "SID", tok, now=1000 + media_urls.ttl_seconds() + 1))
            self.assertFalse(media_urls.token_valid(product, "SID", tok[:-1] + "0", now=1001))
            self.assertFalse(media_urls.token_valid(product, "SID", None))
            self.assertEqual((product / "media_url.key").stat().st_mode & 0o777, 0o600)

    def test_sign_payload_only_signs_viewable_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = media_urls.sign_payload(
                Path(tmp),
                {"a": "/media/MINE/session.mp4", "b": ["/media/THEIRS/x.glb"], "c": "/media/MINE/x?t=1"},
                lambda sid: sid == "MINE",
            )
            self.assertIn("?mt=", out["a"])
            self.assertEqual(out["b"], ["/media/THEIRS/x.glb"])
            self.assertIn("?t=1&mt=", out["c"])


class MediaEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name) / "data"
        self.settings = _settings(data)
        session = data / "sessions" / "SID"
        session.mkdir(parents=True)
        (session / "session.mp4").write_bytes(bytes(range(256)) * 4)
        created = register(self.settings.product_dir, "a@example.com", "password123", accepted_terms=True)
        self.user_id = created["user"]["user_id"]
        add_capture(self.settings.product_dir, user_id=self.user_id, session_id="SID", filename="a.mp4")
        patches = [
            mock.patch.object(chat_server, "SETTINGS", self.settings),
            mock.patch.object(chat_server, "DATA_DIR", data),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _get(self, path: str, headers: dict | None = None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def test_plain_url_is_private(self):
        status, _, _ = self._get("/media/SID/session.mp4")
        self.assertEqual(status, 403)

    def test_signed_url_plays_with_range(self):
        url = media_urls.sign_url(self.settings.product_dir, "/media/SID/session.mp4")
        status, headers, body = self._get(url, {"Range": "bytes=10-19"})
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], "bytes 10-19/1024")
        self.assertEqual(body, bytes(range(10, 20)))
        status, _, body = self._get(url)
        self.assertEqual((status, len(body)), (200, 1024))
        status, _, _ = self._get(url, {"Range": "bytes=5000-"})
        self.assertEqual(status, 416)

    def test_signed_url_dies_with_the_capture(self):
        url = media_urls.sign_url(self.settings.product_dir, "/media/SID/session.mp4")
        delete_capture(self.settings.product_dir, self.user_id, "SID")
        status, _, _ = self._get(url)
        self.assertEqual(status, 403)

    def test_token_for_one_capture_does_not_open_another(self):
        other = self.settings.data_dir / "sessions" / "OTHER"
        other.mkdir(parents=True)
        (other / "session.mp4").write_bytes(b"x")
        add_capture(self.settings.product_dir, user_id="someone", session_id="OTHER", filename="b.mp4")
        tok = media_urls.make_token(self.settings.product_dir, "SID")
        status, _, _ = self._get(f"/media/OTHER/session.mp4?mt={tok}")
        self.assertEqual(status, 403)

    def test_static_cannot_escape_the_ui_folder(self):
        for path in ("/static/../chat_server.py", "/static/../../../deploy/env.product.example", "/static/../../../../../../etc/passwd"):
            conn = __import__("http.client").client.HTTPConnection("127.0.0.1", self.server.server_address[1])
            conn.request("GET", path)  # sent as-is, no client-side normalisation
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 404, path)
            self.assertNotIn(b"root:", body)
        status, _, _ = self._get("/static/billing.css")
        self.assertEqual(status, 200)

    def test_deleted_swing_link_says_gone_to_its_owner_only(self):
        from motion_agent.app.accounts import login

        delete_capture(self.settings.product_dir, self.user_id, "SID")
        token = login(self.settings.product_dir, "a@example.com", "password123")["token"]
        status, _, body = self._get("/api/session?session_id=SID", {"Authorization": token})
        self.assertEqual(status, 410)
        self.assertTrue(json.loads(body)["deleted"])
        register(self.settings.product_dir, "b@example.com", "password123", accepted_terms=True)
        other = login(self.settings.product_dir, "b@example.com", "password123")["token"]
        status, _, _ = self._get("/api/session?session_id=SID", {"Authorization": other})
        self.assertEqual(status, 403)

    def test_json_responses_sign_media_for_the_owner_only(self):
        handler = mock.Mock()
        handler.headers = {}
        handler.wfile.write = mock.Mock()
        chat_server._json_response(handler, 200, {"video_url_path": "/media/SID/session.mp4"})
        body = json.loads(handler.wfile.write.call_args[0][0])
        self.assertEqual(body["video_url_path"], "/media/SID/session.mp4")
        with mock.patch("motion_agent.app.product_api.current_user", return_value={"user_id": self.user_id}):
            chat_server._json_response(handler, 200, {"video_url_path": "/media/SID/session.mp4"})
        body = json.loads(handler.wfile.write.call_args[0][0])
        self.assertIn("?mt=", body["video_url_path"])


if __name__ == "__main__":
    unittest.main()
