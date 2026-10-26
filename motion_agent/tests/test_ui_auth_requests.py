"""The page must send the login header to every endpoint that only shows a capture to its owner.

Without it a signed-in user's own swing answers 403 and the fix card never loads.
"""

from __future__ import annotations

import re
import unittest

from motion_agent.app import chat_server

OWNER_SCOPED = ("/api/sport_analysis", "/api/golf_phases", "/api/coach_briefs", "/api/smplx_mesh_clip", "/api/session")


class OwnerScopedRequestsTests(unittest.TestCase):
    def test_every_owner_scoped_request_sends_auth(self):
        html = (chat_server.UI_DIR / "index.html").read_text(encoding="utf-8")
        lines = html.splitlines()
        missing = []
        for i, line in enumerate(lines):
            for path in OWNER_SCOPED:
                if f'"{path}' not in line:
                    continue
                # The URL may sit on the line after `fetch(`, or be built here and fetched a few lines below.
                window = "\n".join(lines[max(0, i - 2) : i + 8])
                if "fetch(" not in window:
                    continue
                call = window[window.index("fetch(") :]
                call = call[: call.index(";") + 1] if ";" in call else call
                if "authHeaders(" not in call:
                    missing.append(f"line {i + 1}: {path}")
        self.assertEqual(missing, [])

    def test_the_endpoints_are_still_owner_scoped(self):
        source = (chat_server.__file__ and open(chat_server.__file__, encoding="utf-8").read()) or ""
        for path in ("/api/sport_analysis", "/api/golf_phases", "/api/coach_briefs", "/api/smplx_mesh_clip"):
            block = source[source.index(f'path == "{path}"') :][:600]
            self.assertTrue(re.search(r"_scoped_video_id\(self", block), path)


if __name__ == "__main__":
    unittest.main()
