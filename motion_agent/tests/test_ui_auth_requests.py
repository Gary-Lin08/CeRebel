"""The page must send the login header to every endpoint that only shows a capture to its owner.

Without it a signed-in user's own swing answers 403 and the fix card never loads.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from motion_agent.app import chat_server
from motion_agent.app.accounts import register
from motion_agent.app.config import Settings
from motion_agent.app.library import add_capture

# The only /api endpoints the page may call without the login header: none of them reads a capture.
PUBLIC = {
    "/api/sports",  # sport catalog
    "/api/billing",  # landing-page plans; billing.js asks with the header for the signed-in view
    "/api/system_prompt",  # operator password, not an account
    "/api/auth/register",
    "/api/auth/login",
}
# Server helpers that limit a route to the capture's owner (or the shared demo).
CAPTURE_GATES = ("_scoped_video_id(", "user_may_view_session(", "media_allowed(")
# Endpoints that read one capture by video_id; each must stay behind _scoped_video_id.
CAPTURE_READS = (
    "/api/golf_phases",
    "/api/sport_analysis",
    "/api/coach_briefs",
    "/api/smplx_mesh_status",
    "/api/smplx_mesh_clip",
)


def _fetch_calls(source: str):
    """Yield (line, args, text before the call) for every fetch(...) in the page."""
    for m in re.finditer(r"\bfetch\(", source):
        i, depth, quote = m.end(), 1, None
        while i < len(source) and depth:
            ch = source[i]
            if quote:
                if ch == "\\":
                    i += 1
                elif ch == quote:
                    quote = None
            elif ch in "\"'`":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        yield source.count("\n", 0, m.start()) + 1, source[m.end() : i - 1], source[: m.start()]


def _api_paths(args: str, before: str) -> set[str]:
    """/api paths a fetch may hit, following `fetch(url, ...)` back to `const url = "/api/..."`."""
    paths = set(re.findall(r"[\"'`](/api/[\w/-]+)", args))
    var = re.match(r"\s*([A-Za-z_$][\w$]*)\s*[,)]?", args)
    if not paths and var:
        assigned = list(re.finditer(rf"\b(?:const|let|var)\s+{re.escape(var.group(1))}\s*=([^;]*);", before))
        if assigned:
            paths = set(re.findall(r"[\"'`](/api/[\w/-]+)", assigned[-1].group(1)))
    return paths


def _sends_login(args: str, before: str) -> bool:
    if "authHeaders(" in args:
        return True
    # `headers` built a few lines up, e.g. const headers = authHeaders({...}); fetch(url, { headers })
    named = re.search(r"\bheaders\s*:\s*([A-Za-z_$][\w$]*)\s*[,}]", args) or re.search(r"[{,]\s*(headers)\s*[,}]", args)
    if not named:
        return False
    return re.search(rf"\b(?:const|let|var)\s+{re.escape(named.group(1))}\s*=\s*authHeaders\(", before[-4000:]) is not None


def _route_block(source: str, path: str) -> str:
    start = source.index(f'"{path}"')
    end = re.compile(r"\n {8}if (?:parsed\.)?path\b").search(source, start)
    return source[start : end.start() if end else len(source)]


class PageSendsLoginHeaderTests(unittest.TestCase):
    def setUp(self):
        self.html = (chat_server.UI_DIR / "index.html").read_text(encoding="utf-8")

    def test_every_api_request_sends_the_login_header(self):
        missing, seen = [], set()
        for line, args, before in _fetch_calls(self.html):
            paths = _api_paths(args, before)
            seen |= paths
            if paths - PUBLIC and not _sends_login(args, before):
                missing.append(f"index.html:{line} {sorted(paths - PUBLIC)}")
        self.assertEqual(missing, [], "add { headers: authHeaders() } to these requests")
        # Guard the scanner itself: it must still see the requests the report page depends on.
        for path in ("/api/session", "/api/golf_phases", "/api/sport_analysis", "/api/coach_briefs", "/api/smplx_mesh_clip", "/api/chat/stream"):
            self.assertIn(path, seen)

    def test_scanner_catches_a_request_without_the_header(self):
        bad = 'const url = "/api/golf_phases?video_id=" + sid;\nconst r = await fetch(url);'
        good = 'const headers = authHeaders({ A: 1 });\nawait fetch("/api/chat/stream", { method: "POST", headers, body });'
        (_, args, before), = _fetch_calls(bad)
        self.assertEqual(_api_paths(args, before), {"/api/golf_phases"})
        self.assertFalse(_sends_login(args, before))
        (_, args, before), = _fetch_calls(good)
        self.assertTrue(_sends_login(args, before))


class EndpointsStayOwnerScopedTests(unittest.TestCase):
    def setUp(self):
        self.source = Path(chat_server.__file__).read_text(encoding="utf-8")

    def test_capture_reads_check_the_caller(self):
        for path in CAPTURE_READS:
            self.assertIn("_scoped_video_id(self", _route_block(self.source, path), path)

    def test_public_endpoints_read_no_capture(self):
        for path in PUBLIC:
            block = _route_block(self.source, path)
            self.assertFalse([g for g in CAPTURE_GATES if g in block], path)


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


def _echo(video_id=None, **_):
    return {"ok": True, "video_id": video_id}


def _echo_dir(session_dir, *_, **__):
    return {"ok": True, "video_id": Path(session_dir).name}


class OwnerScopedEndpointTests(unittest.TestCase):
    """Real HTTP round trips; the analysis itself is stubbed, only the access decision is under test."""

    URLS = (
        "/api/golf_phases?video_id={sid}",
        "/api/sport_analysis?sport=golf&video_id={sid}",
        "/api/coach_briefs?sport=golf&video_id={sid}",
        "/api/smplx_mesh_status?video_id={sid}",
        "/api/smplx_mesh_clip?video_id={sid}&t0=0&t1=1&max_frames=160&muscles=1",
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = Path(self.tmp.name) / "data"
        settings = _settings(data)
        for sid in ("MINE", "THEIRS"):
            (data / "sessions" / sid / "ik").mkdir(parents=True)
            (data / "sessions" / sid / "ik" / "opensim-motion.json").write_text("{}")
        me = register(settings.product_dir, "me@example.com", "password123", accepted_terms=True)
        them = register(settings.product_dir, "them@example.com", "password123", accepted_terms=True)
        add_capture(settings.product_dir, user_id=me["user"]["user_id"], session_id="MINE", filename="a.mp4", status="ready")
        add_capture(settings.product_dir, user_id=them["user"]["user_id"], session_id="THEIRS", filename="b.mp4", status="ready")
        self.my_login = {"Authorization": "Bearer " + me["token"]}
        self.analysis = mock.Mock(side_effect=_echo)
        self.mesh = mock.Mock(side_effect=_echo_dir)
        patches = [
            mock.patch.dict(os.environ, {"CEREBEL_DEMO_SESSION_ID": ""}),
            mock.patch.object(chat_server, "SETTINGS", settings),
            mock.patch.object(chat_server, "DATA_DIR", data),
            mock.patch.object(chat_server, "get_golf_phase_cards", self.analysis),
            mock.patch.object(chat_server, "get_sport_analysis", lambda sport, **kw: self.analysis(**kw)),
            mock.patch("motion_agent.app.coach_briefs.build_coach_briefs", self.analysis),
            mock.patch.object(chat_server, "smplx_mesh_status", self.mesh),
            mock.patch.object(chat_server, "get_smplx_mesh_clip", self.mesh),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _get(self, path: str, headers: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_owner_with_login_header_reads_their_own_swing(self):
        for url in self.URLS:
            status, body = self._get(url.format(sid="MINE"), self.my_login)
            self.assertEqual((status, body.get("video_id")), (200, "MINE"), url)

    def test_owner_without_login_header_is_refused(self):
        # What the page got before it sent the header: the owner's own swing looked private.
        for url in self.URLS:
            status, body = self._get(url.format(sid="MINE"))
            self.assertEqual((status, body.get("message")), (403, "This capture is private"), url)

    def test_someone_elses_swing_stays_private(self):
        for url in self.URLS:
            for headers in (None, self.my_login):
                status, body = self._get(url.format(sid="THEIRS"), headers)
                self.assertEqual((status, body.get("message")), (403, "This capture is private"), (url, headers))
        # Refused before any analysis ran for that capture.
        self.assertNotIn("THEIRS", str(self.analysis.call_args_list) + str(self.mesh.call_args_list))


if __name__ == "__main__":
    unittest.main()
