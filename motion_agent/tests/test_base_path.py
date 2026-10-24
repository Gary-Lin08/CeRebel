import unittest

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


if __name__ == "__main__":
    unittest.main()
