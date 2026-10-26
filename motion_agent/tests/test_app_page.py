"""The customer app page (/app): served with the surface tag and base path, linked from the old page.

The page itself is static HTML/JS; these tests pin what the server and the old page promise it
(route, base-path rewrite, upload origin, the classic switch) and what the script must keep doing
(the login header on every request, the same login key as the old page, assets that exist).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from motion_agent.app import chat_server
from motion_agent.tests.test_ui_auth_requests import PUBLIC, _api_paths, _fetch_calls, _sends_login

UI = chat_server.UI_DIR


def _serve(operator: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
    server.operator = operator
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _get(server: ThreadingHTTPServer, path: str) -> tuple[int, str]:
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class AppRouteTests(unittest.TestCase):
    def setUp(self):
        self.server = _serve()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_app_page_is_served(self):
        for path in ("/app", "/app/"):
            code, html = _get(self.server, path)
            self.assertEqual(code, 200, path)
            self.assertIn('id="vReport"', html)
            self.assertIn('src="/static/app.js"', html)

    def test_customer_port_marks_the_app_page(self):
        with mock.patch.object(chat_server, "OPERATOR_PORT", 8093):
            code, html = _get(self.server, "/app")
        self.assertEqual(code, 200)
        self.assertIn(chat_server.SURFACE_META.format("customer"), html)

    def test_base_path_prefixes_the_app_page_and_its_script(self):
        with mock.patch.object(chat_server, "BASE_PATH", "/playground"):
            code, html = _get(self.server, "/playground/app")
            self.assertEqual(code, 200)
            self.assertIn('src="/playground/static/app.js"', html)
            self.assertIn('href="/playground/privacy"', html)
            self.assertIn('href="/playground/"', html)
            code, js = _get(self.server, "/playground/static/app.js")
            self.assertEqual(code, 200)
        self.assertIn('fetch("/playground/api/library"', js)
        self.assertIn('xhr.open("POST", "/playground/api/upload")', js)
        self.assertNotIn('"/api/', js)

    def test_upload_goes_to_the_upload_origin_behind_the_proxy(self):
        with mock.patch.object(chat_server, "BASE_PATH", "/playground"), mock.patch.object(
            chat_server, "UPLOAD_ORIGIN", "https://app.example.com"
        ):
            code, js = _get(self.server, "/playground/static/app.js")
        self.assertEqual(code, 200)
        self.assertIn('xhr.open("POST", "https://app.example.com/api/upload")', js)

    def test_index_carries_the_app_switch(self):
        code, html = _get(self.server, "/")
        self.assertEqual(code, 200)
        self.assertIn(chat_server.APP_META.format(chat_server.APP_UI), html)
        with mock.patch.object(chat_server, "APP_UI", "classic"):
            _, html = _get(self.server, "/")
        self.assertIn(chat_server.APP_META.format("classic"), html)
        self.assertNotIn(chat_server.APP_META.format("new"), html)
        with mock.patch.object(chat_server, "APP_UI", "new"):
            _, html = _get(self.server, "/")
        self.assertIn(chat_server.APP_META.format("new"), html)

    def test_index_links_into_the_app_with_the_base_path(self):
        with mock.patch.object(chat_server, "BASE_PATH", "/playground"):
            _, html = _get(self.server, "/playground/")
        self.assertIn('window.location.replace("/playground/app"', html)
        self.assertIn('window.location.href = "/playground/app"', html)
        self.assertIn('href="/playground/app#sample"', html)


class AppSwitchSettingTests(unittest.TestCase):
    def test_default_is_the_app_page(self):
        self.assertEqual(chat_server.APP_UI, "new")

    def test_with_base_prefixes_app_links(self):
        out = chat_server.with_base('<a href="/app#sample">x</a> go("/app")', "/playground")
        self.assertIn('href="/playground/app#sample"', out)
        self.assertIn('go("/playground/app")', out)


class AppScriptTests(unittest.TestCase):
    def setUp(self):
        self.js = (UI / "app.js").read_text(encoding="utf-8")
        self.html = (UI / "app.html").read_text(encoding="utf-8")
        self.index = (UI / "index.html").read_text(encoding="utf-8")

    def test_every_api_request_sends_the_login_header(self):
        missing, seen = [], set()
        for line, args, before in _fetch_calls(self.js):
            paths = _api_paths(args, before)
            seen |= paths
            if paths and not _sends_login(args, before):
                missing.append(f"app.js:{line} {sorted(paths)}")
        self.assertEqual(missing, [], "add { headers: authHeaders() } to these requests")
        for path in ("/api/session", "/api/sport_analysis", "/api/library", "/api/chat/stream", "/api/compare"):
            self.assertIn(path, seen)
        self.assertTrue(PUBLIC)  # the shared scanner is the one the old page is checked with

    def test_the_upload_sends_the_login_header(self):
        block = self.js[self.js.index("function uploadWithProgress") :]
        block = block[: block.index("\n  }\n")]
        self.assertIn("authHeaders()", block)
        self.assertIn("setRequestHeader", block)

    def test_same_login_key_as_the_old_page(self):
        key = re.search(r'const AUTH_KEY = "([^"]+)"', self.js).group(1)
        self.assertIn(f'const AUTH_KEY = "{key}"', self.index)
        self.assertIn(f'localStorage.getItem("{key}")', self.index)

    def test_globals_the_shared_modules_call(self):
        for name in ("authHeaders", "showToast", "refreshLibrary", "loadSession", "openAccountPanel", "selectFix", "escapeHtml", "addBubble", "showConsumerChat"):
            self.assertRegex(self.js, rf"\nfunction {name}\(", name)
        for name in ("authToken", "activeSessionId", "session"):
            self.assertRegex(self.js, rf"\nlet {name}\b", name)

    def test_page_assets_exist(self):
        refs = re.findall(r'(?:src|href)="/static/([^"]+)"', self.html)
        self.assertIn("app.js", refs)
        for ref in refs:
            self.assertTrue((UI / ref).is_file(), ref)

    def test_ids_the_script_reads_exist_in_the_page(self):
        ids = set(re.findall(r'\$\("([A-Za-z][\w-]*)"\)', self.js))
        declared = set(re.findall(r'\bid="([^"]+)"', self.html))
        self.assertEqual(sorted(ids - declared), [])

    def test_video_overlay_mounts_on_the_report_video(self):
        # skeleton-overlay.js draws on the video's parent; the report's video sits in the stage box.
        self.assertRegex(self.html, r'<div class="cx-stage__box" id="stageBox">\s*<video id="fixVideo"')

    def test_voice_coach_loads_after_the_app_script(self):
        head = self.html[: self.html.index("<body")]
        self.assertIn('href="/static/voice-coach.css"', head)
        self.assertLess(self.html.index('src="/static/app.js"'), self.html.index('src="/static/voice-coach.js"'))

    def test_globals_the_voice_coach_calls(self):
        # voice-coach.js names the page globals it calls at run time in its header comment.
        vc = (UI / "voice-coach.js").read_text(encoding="utf-8")
        names = [n.strip() for n in re.search(r"globals \(([^)]*)\)", vc).group(1).replace("\n", " ").split(",")]
        self.assertIn("addBubble", names)
        for name in names:
            if name == "cerebelBilling":
                self.assertIn('src="/static/billing.js"', self.html)
            else:
                self.assertRegex(self.js, rf"\n(?:function {name}\(|let {name}\b)", name)

    def test_redirect_runs_before_the_landing_renders(self):
        head = self.index[: self.index.index("<body")]
        self.assertIn('meta[name="cerebel-app"]', head)
        self.assertIn(chat_server.APP_META.format("classic"), head)
        self.assertIn('window.location.replace("/app"', head)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class AppHelperTests(unittest.TestCase):
    """The pure helpers in app.js, run in Node with just enough of a browser stubbed out."""

    @classmethod
    def setUpClass(cls):
        harness = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const doc = { getElementById: () => null, addEventListener() {}, readyState: "loading", querySelectorAll: () => [] };
const win = { location: { search: "", hash: process.argv[2], pathname: "/app" }, history: { replaceState() {} }, addEventListener() {}, matchMedia: () => ({ matches: false }) };
const t = new Function("document", "window", "localStorage", "URLSearchParams", src + "\nreturn CxApp._test;")(doc, win, { getItem: () => "" }, URLSearchParams);
const out = {
  drill: t.drillParts("Pump drill. Pause at the top with the lead arm extended, pump down halfway twice, then swing through. Eight reps."),
  plainDrill: t.drillParts("Slow swings to a pause at the top"),
  values: [t.formatValue(23.77, "°"), t.formatValue(6.06, "cm"), t.formatValue(43.62, "cm"), t.formatValue(null, "°")],
  series: [t.seriesValue({ times: [0, 1, 2], values: [0, 10, 20] }, 1.5), t.seriesValue({ times: [0, 1], values: [5, 7] }, 9)],
  stripped: t.stripAnalysis("Keep the hip back.\n```cerebel-analysis\n{\"findings\": []}\n```"),
  route: t.parseRoute(),
};
process.stdout.write(JSON.stringify(out));
"""

        def run(hash_):
            done = subprocess.run(
                ["node", "-e", harness, str(UI / "app.js"), hash_], capture_output=True, text=True, timeout=30, check=True
            )
            return json.loads(done.stdout)

        cls.node = staticmethod(run)
        cls.out = run("#/swing/5032CEE6-47F7-41AD-9574-10ADFB348003/drill")

    def test_drill_name_and_steps(self):
        self.assertEqual(self.out["drill"]["name"], "Pump drill")
        self.assertEqual(
            self.out["drill"]["steps"],
            ["Pause at the top with the lead arm extended, pump down halfway twice, then swing through.", "Eight reps."],
        )
        self.assertEqual(self.out["plainDrill"], {"name": "Slow swings to a pause at the top", "steps": []})

    def test_values_are_rounded_for_reading(self):
        self.assertEqual(self.out["values"], ["24°", "6.1 cm", "44 cm", "—"])

    def test_series_interpolates_and_clamps(self):
        self.assertEqual(self.out["series"], [15, 7])

    def test_coach_analysis_block_is_hidden(self):
        self.assertEqual(self.out["stripped"], "Keep the hip back.")

    def test_routes(self):
        self.assertEqual(self.out["route"], {"name": "swing", "sid": "5032CEE6-47F7-41AD-9574-10ADFB348003", "tab": "drill"})
        self.assertEqual(self.node("#/sample")["route"], {"name": "sample"})
        self.assertEqual(self.node("#signup")["route"], {"name": "sample", "auth": "signup"})
        # Signed out, home falls back to the sample; a bad id is not a swing route.
        self.assertEqual(self.node("#/")["route"], {"name": "sample"})
        self.assertEqual(self.node("#/swing/../../etc")["route"], {"name": "sample"})


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class AppVoiceModeTests(unittest.TestCase):
    """What the mic button and the call card do: a live call when voice-coach.js can start one in
    this browser, else dictation where the browser has speech recognition, else nothing."""

    HARNESS = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8");
const doc = { getElementById: () => null, addEventListener() {}, readyState: "loading", querySelectorAll: () => [] };
const out = {};
for (const [name, extra] of Object.entries(JSON.parse(process.argv[2]))) {
  const win = Object.assign({ location: { search: "", hash: "", pathname: "/app" }, history: { replaceState() {} }, addEventListener() {}, matchMedia: () => ({ matches: false }) }, extra);
  if (extra.voice) win.cerebelVoice = { start() {}, state: () => ({ available: extra.voice === "available" }) };
  if (extra.voice === "no-start") win.cerebelVoice = { refresh() {}, hangUp() {} };
  const t = new Function("document", "window", "localStorage", "URLSearchParams", src + "\nreturn CxApp._test;")(doc, win, { getItem: () => "" }, URLSearchParams);
  out[name] = t.voiceMode();
}
process.stdout.write(JSON.stringify(out));
"""

    def test_voice_mode(self):
        cases = {
            "nothing": {},
            "dictation only": {"webkitSpeechRecognition": True},
            "live call": {"voice": "available"},
            "live call unavailable": {"voice": "unavailable", "webkitSpeechRecognition": True},
            "voice script without start": {"voice": "no-start", "SpeechRecognition": True},
        }
        done = subprocess.run(
            ["node", "-e", self.HARNESS, str(UI / "app.js"), json.dumps(cases)], capture_output=True, text=True, timeout=30, check=True
        )
        self.assertEqual(
            json.loads(done.stdout),
            {
                "nothing": "",
                "dictation only": "dictate",
                "live call": "live",
                "live call unavailable": "dictate",
                "voice script without start": "dictate",
            },
        )


if __name__ == "__main__":
    unittest.main()
