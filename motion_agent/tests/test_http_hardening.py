"""HTTP hardening: explicit CORS allowlist and opaque error payloads."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from motion_agent.app import chat_server


def _handler(origin: str | None) -> mock.Mock:
    handler = mock.Mock()
    handler.headers = {"Origin": origin} if origin else {}
    return handler


class CorsTests(unittest.TestCase):
    def test_unknown_origin_gets_no_header(self):
        with mock.patch.object(chat_server, "ALLOWED_ORIGINS", frozenset({"https://app.example"})):
            handler = _handler("https://evil.example")
            chat_server._send_cors_headers(handler)
            handler.send_header.assert_not_called()

    def test_allowed_origin_is_echoed(self):
        with mock.patch.object(chat_server, "ALLOWED_ORIGINS", frozenset({"https://app.example"})):
            handler = _handler("https://app.example/")
            chat_server._send_cors_headers(handler)
            handler.send_header.assert_any_call("Access-Control-Allow-Origin", "https://app.example")
            handler.send_header.assert_any_call("Vary", "Origin")

    def test_same_origin_requests_need_no_header(self):
        handler = _handler(None)
        chat_server._send_cors_headers(handler)
        handler.send_header.assert_not_called()

    def test_no_wildcard_left(self):
        source = Path(chat_server.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"Access-Control-Allow-Origin", "*"', source)


class ErrorPayloadTests(unittest.TestCase):
    def test_internal_error_hides_details(self):
        with self.assertLogs(chat_server.LOG, "ERROR"):
            payload = chat_server._internal_error(RuntimeError("/root/secret/path exploded"), "test")
        text = json.dumps(payload)
        self.assertNotIn("exploded", text)
        self.assertNotIn("traceback", text)
        self.assertTrue(payload["error"])
        self.assertEqual(len(payload["error_id"]), 12)


if __name__ == "__main__":
    unittest.main()
