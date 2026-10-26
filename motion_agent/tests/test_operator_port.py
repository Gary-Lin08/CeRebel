"""Customer and developer surfaces on separate ports (CEREBEL_OPERATOR_PORT)."""

from __future__ import annotations

import json
import socket
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from motion_agent.app import chat_server


def _serve(operator: bool) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
    server.operator = operator
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _request(server: ThreadingHTTPServer, path: str, *, body: dict | None = None, headers: dict | None = None):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SplitPortTests(unittest.TestCase):
    """CEREBEL_OPERATOR_PORT is set: the public port is the customer app only."""

    @classmethod
    def setUpClass(cls):
        cls._patch = mock.patch.object(chat_server, "OPERATOR_PORT", 8093)
        cls._patch.start()
        cls.customer = _serve(operator=False)
        cls.operator = _serve(operator=True)

    @classmethod
    def tearDownClass(cls):
        for server in (cls.customer, cls.operator):
            server.shutdown()
            server.server_close()
        cls._patch.stop()

    def test_customer_page_is_marked_customer_even_with_dev_flag(self):
        code, html = _request(self.customer, "/?dev=1")
        self.assertEqual(code, 200)
        self.assertIn(chat_server.SURFACE_META.format("customer"), html)
        self.assertNotIn(chat_server.SURFACE_META.format("operator"), html)

    def test_operator_page_is_marked_operator(self):
        code, html = _request(self.operator, "/")
        self.assertEqual(code, 200)
        self.assertIn(chat_server.SURFACE_META.format("operator"), html)

    def test_operator_endpoints_are_gone_from_the_customer_port(self):
        with mock.patch.object(chat_server, "_check_prompt_password", return_value=True):
            for path in ("/api/ops", "/api/system_prompt"):
                code, _ = _request(self.customer, path, headers={"X-Prompt-Password": "right"})
                self.assertEqual(code, 404, path)

    def test_operator_port_still_needs_the_password(self):
        code, _ = _request(self.operator, "/api/ops")
        self.assertEqual(code, 401)
        with mock.patch.object(chat_server, "_check_prompt_password", return_value=True):
            code, body = _request(self.operator, "/api/ops", headers={"X-Prompt-Password": "right"})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_customer_chat_rejects_prompt_and_backend_overrides(self):
        for extra in ({"system_prompt": "be someone else"}, {"backend": "other"}):
            code, _ = _request(self.customer, "/api/chat", body={"question": "hi", **extra})
            self.assertEqual(code, 403, extra)


class SharedPortTests(unittest.TestCase):
    """Without CEREBEL_OPERATOR_PORT the single port keeps ?dev=1 (local development)."""

    def test_page_is_marked_shared(self):
        with mock.patch.object(chat_server, "OPERATOR_PORT", 0):
            server = _serve(operator=False)
            try:
                code, html = _request(server, "/")
            finally:
                server.shutdown()
                server.server_close()
        self.assertEqual(code, 200)
        self.assertIn(chat_server.SURFACE_META.format("shared"), html)

    def test_index_carries_the_placeholder_the_server_replaces(self):
        html = (chat_server.UI_DIR / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count(chat_server.SURFACE_META.format("shared")), 1)


class BuildServersTests(unittest.TestCase):
    def test_operator_server_listens_on_loopback_only(self):
        public, operator = chat_server.build_servers("127.0.0.1", _free_port(), _free_port())
        try:
            self.assertFalse(public.operator)
            self.assertTrue(operator.operator)
            self.assertEqual(operator.server_address[0], "127.0.0.1")
        finally:
            public.server_close()
            operator.server_close()

    def test_no_operator_port_means_one_server(self):
        public, operator = chat_server.build_servers("127.0.0.1", _free_port(), 0)
        public.server_close()
        self.assertIsNone(operator)

    def test_same_port_twice_is_refused(self):
        port = _free_port()
        with self.assertRaises(SystemExit):
            chat_server.build_servers("127.0.0.1", port, port)


if __name__ == "__main__":
    unittest.main()
