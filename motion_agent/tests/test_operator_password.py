"""Operator password: environment only, constant-time compare, header only."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

from motion_agent.app import chat_server
from motion_agent.app.config import Settings


def _settings(password: str | None) -> Settings:
    return Settings(
        data_dir=Path("/nonexistent-cerebel-test-data"),
        host="127.0.0.1",
        port=1,
        backend="mock",
        prompt_password=password,
        max_upload_bytes=1,
        invite_daily_limit=1,
    )


class OperatorPasswordTests(unittest.TestCase):
    def test_no_builtin_fallback(self):
        self.assertFalse(hasattr(chat_server, "_LEGACY_PROMPT_PASSWORD"))

    def test_locked_when_unset(self):
        with mock.patch.object(chat_server, "SETTINGS", _settings(None)):
            self.assertFalse(chat_server._check_prompt_password("anything"))
            self.assertFalse(chat_server._check_prompt_password(""))
            self.assertFalse(chat_server._check_prompt_password(None))

    def test_matches_configured_password(self):
        with mock.patch.object(chat_server, "SETTINGS", _settings("correct-horse")):
            self.assertTrue(chat_server._check_prompt_password("correct-horse"))
            self.assertFalse(chat_server._check_prompt_password("wrong"))
            self.assertFalse(chat_server._check_prompt_password(None))

    def test_query_string_is_ignored(self):
        handler = mock.Mock()
        handler.headers = {}
        parsed = urlparse("/api/ops?password=correct-horse")
        self.assertIsNone(chat_server._password_from_request(handler, parsed))
        handler.headers = {"X-Prompt-Password": " correct-horse "}
        self.assertEqual(chat_server._password_from_request(handler, parsed), "correct-horse")
