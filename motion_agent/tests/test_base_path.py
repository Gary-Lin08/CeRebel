import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from motion_agent.app import chat_server
from motion_agent.app.chat_server import with_base


class TestBasePath(unittest.TestCase):
    def test_absolute_app_urls_get_the_prefix(self):
        src = 'fetch("/api/me"); <script src="/static/a.js"></script> <a href="/terms">'
        out = with_base(src, "/playground")
        self.assertIn('fetch("/playground/api/me")', out)
        self.assertIn('src="/playground/static/a.js"', out)
        self.assertIn('href="/playground/terms"', out)

    def test_home_link_and_css_url(self):
        out = with_base("<a href=\"/\">home</a> url(/static/x.png) `/media/${id}`", "/playground")
        self.assertIn('href="/playground/"', out)
        self.assertIn("url(/playground/static/x.png)", out)
        self.assertIn("`/playground/media/${id}`", out)

    def test_no_prefix_leaves_text_alone(self):
        src = 'fetch("/api/me")'
        self.assertEqual(with_base(src, ""), src)
        self.assertEqual(with_base('href="https://x.com/api/y"', "/playground"), 'href="https://x.com/api/y"')


class TestStaticUnderBasePath(unittest.TestCase):
    """Vendored files must reach the browser byte for byte and stay cached; app files get the prefix."""

    @classmethod
    def setUpClass(cls):
        cls._patch = mock.patch.object(chat_server, "BASE_PATH", "/playground")
        cls._patch.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._patch.stop()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.server.server_address[1]}{path}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read(), resp.headers

    def test_vendored_loader_is_not_rewritten(self):
        rel = "vendor/pose/0.10.14/wasm/vision_wasm_internal.js"
        body, headers = self._get("/playground/static/" + rel)
        self.assertEqual(body, (chat_server.UI_DIR / rel).read_bytes())
        self.assertIn('"/"', body.decode("utf-8"))
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))

    def test_vendored_module_and_model(self):
        _, headers = self._get("/playground/static/vendor/pose/0.10.14/vision_bundle.mjs")
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        body, headers = self._get("/playground/static/vendor/pose/pose_landmarker_lite_f16_v1.task")
        self.assertEqual(len(body), (chat_server.UI_DIR / "vendor/pose/pose_landmarker_lite_f16_v1.task").stat().st_size)
        self.assertIn("max-age=31536000", headers["Cache-Control"])

    def test_app_script_still_gets_prefix_and_no_cache(self):
        body, headers = self._get("/playground/static/swing_trim.js")
        text = body.decode("utf-8")
        self.assertIn('"/playground/static/vendor/pose/0.10.14/vision_bundle.mjs"', text)
        self.assertEqual(headers["Cache-Control"], "no-cache")


if __name__ == "__main__":
    unittest.main()
