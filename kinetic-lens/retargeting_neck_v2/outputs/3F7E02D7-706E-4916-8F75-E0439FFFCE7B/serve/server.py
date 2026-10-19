#!/usr/bin/env python3
"""Serve the old-vs-new neck comparison UI on port 8090."""

from __future__ import annotations

import mimetypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = "0.0.0.0"
PORT = 8090


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def guess_type(self, path: str) -> str:  # type: ignore[override]
        if path.endswith(".js") or path.endswith(".module.js"):
            return "text/javascript; charset=utf-8"
        guessed, _ = mimetypes.guess_type(path)
        return guessed or "application/octet-stream"


if __name__ == "__main__":
    # Ensure .js maps correctly even if system mime is wrong.
    mimetypes.add_type("text/javascript", ".js")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"neck comparison UI on http://{HOST}:{PORT}/", flush=True)
    print(f"root={ROOT}", flush=True)
    server.serve_forever()
