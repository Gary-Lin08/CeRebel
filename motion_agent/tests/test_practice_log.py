"""Marking a fix's drill as practiced on your own capture."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from motion_agent.app import chat_server
from motion_agent.app.config import Settings
from motion_agent.app.library import (
    PRACTICE_LOG_LIMIT,
    add_capture,
    delete_capture,
    list_captures,
    log_practice,
    purge_user_rows,
)
from motion_agent.app.product_api import log_practice as handle_practice


def _settings(root: Path) -> Settings:
    return Settings(
        data_dir=root,
        host="127.0.0.1",
        port=1,
        backend="mock",
        prompt_password=None,
        max_upload_bytes=1024,
        invite_daily_limit=5,
    )


class PracticeStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.product = Path(self._tmp.name)
        add_capture(self.product, user_id="u1", session_id="S1", filename="a.mp4")
        add_capture(self.product, user_id="u2", session_id="S2", filename="b.mp4")

    def tearDown(self):
        self._tmp.cleanup()

    def test_counts_per_fix_on_own_capture(self):
        self.assertEqual(log_practice(self.product, "u1", "S1", "trail-knee"), 1)
        self.assertEqual(log_practice(self.product, "u1", "S1", "trail-knee"), 2)
        self.assertEqual(log_practice(self.product, "u1", "S1", "lead-arm"), 1)
        row = list_captures(self.product, "u1")[0]
        self.assertEqual([p["finding_id"] for p in row["practice"]], ["trail-knee", "trail-knee", "lead-arm"])
        self.assertTrue(all(p["at"] for p in row["practice"]))

    def test_someone_elses_or_deleted_capture_is_refused(self):
        self.assertIsNone(log_practice(self.product, "u1", "S2", "trail-knee"))
        delete_capture(self.product, "u1", "S1")
        self.assertIsNone(log_practice(self.product, "u1", "S1", "trail-knee"))

    def test_log_is_capped(self):
        for _ in range(PRACTICE_LOG_LIMIT + 5):
            log_practice(self.product, "u1", "S1", "trail-knee")
        row = list_captures(self.product, "u1")[0]
        self.assertEqual(len(row["practice"]), PRACTICE_LOG_LIMIT)

    def test_account_deletion_removes_the_log(self):
        log_practice(self.product, "u1", "S1", "trail-knee")
        purge_user_rows(self.product, "u1")
        rows = json.loads((self.product / "library.json").read_text(encoding="utf-8"))
        self.assertFalse(any(r.get("user_id") == "u1" for r in rows))


class PracticeHandlerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self._tmp.name))
        add_capture(self.settings.product_dir, user_id="u1", session_id="S1", filename="a.mp4")
        self.user = {"user_id": "u1", "email": "u1@example.com"}

    def tearDown(self):
        self._tmp.cleanup()

    def test_ok(self):
        code, payload = handle_practice(self.settings, self.user, {"session_id": "S1", "finding_id": "trail-knee"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["count"], 1)

    def test_bad_fix_id(self):
        for bad in ("", "Trail Knee", "x" * 80, "../etc", None):
            code, _ = handle_practice(self.settings, self.user, {"session_id": "S1", "finding_id": bad})
            self.assertEqual(code, 400, bad)

    def test_unknown_capture(self):
        code, _ = handle_practice(self.settings, self.user, {"session_id": "NOPE", "finding_id": "trail-knee"})
        self.assertEqual(code, 404)


class PracticeEndpointTests(unittest.TestCase):
    def test_sign_in_required(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
        server.operator = False
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/library/practice",
                data=json.dumps({"session_id": "S1", "finding_id": "trail-knee"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(ctx.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
