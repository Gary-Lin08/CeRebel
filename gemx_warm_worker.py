#!/usr/bin/env python3
"""Persistent GEM-X warm worker — models stay loaded; jobs arrive over TCP.

Protocol (one JSON object per line):
  {"cmd":"ping"}
  {"cmd":"run","session_id":"...","jpeg_dir":"...","output_root":"...","fps":24,"static_cam":true}
  {"cmd":"run","session_id":"...","video_path":"...","output_root":"...","fps":30,"static_cam":true}
  # Prefer jpeg_dir when ready; video_path is resampled to fps (same filter as JPEG extract).

Replies are a single JSON line.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import sys
import threading
from pathlib import Path

GEMX_ROOT = Path(os.environ.get("CEREBEL_GEMX_ROOT", "/root/GEM-X")).resolve()
if str(GEMX_ROOT) not in sys.path:
    sys.path.insert(0, str(GEMX_ROOT))
os.chdir(GEMX_ROOT)

HOST = os.environ.get("CEREBEL_GEMX_WORKER_HOST", "127.0.0.1")
PORT = int(os.environ.get("CEREBEL_GEMX_WORKER_PORT", "61017"))
CKPT = os.environ.get(
    "CEREBEL_GEMX_CKPT",
    str(GEMX_ROOT / "inputs" / "pretrained" / "gem_soma.ckpt"),
)
OUTPUT_ROOT = os.environ.get(
    "CEREBEL_GEMX_OUTPUT_ROOT", str(GEMX_ROOT / "outputs" / "cerebel")
)

LOG = logging.getLogger("cerebel.gemx_worker")

_pipeline = None
_pipeline_lock = threading.Lock()


def _get_pipeline():
    global _pipeline
    with _pipeline_lock:
        if _pipeline is None:
            from scripts.demo.warm_soma_pipeline import WarmSomaPipeline

            _pipeline = WarmSomaPipeline(ckpt_path=CKPT if Path(CKPT).is_file() else None)
        return _pipeline


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline()
        if not raw:
            return
        try:
            req = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._reply({"ok": False, "error": f"bad json: {exc}"})
            return

        cmd = req.get("cmd", "run")
        if cmd == "ping":
            ready = _pipeline is not None
            self._reply({"ok": True, "ready": ready, "loading": not ready})
            return

        if cmd != "run":
            self._reply({"ok": False, "error": f"unknown cmd {cmd}"})
            return

        session_id = req.get("session_id")
        if not session_id:
            self._reply({"ok": False, "error": "session_id required"})
            return

        try:
            pipe = _get_pipeline()
            with _pipeline_lock:
                result = pipe.run(
                    session_id=session_id,
                    output_root=req.get("output_root") or OUTPUT_ROOT,
                    video_path=req.get("video_path"),
                    jpeg_dir=req.get("jpeg_dir"),
                    static_cam=bool(req.get("static_cam", True)),
                    fps=float(req.get("fps") or 30.0),
                )
            self._reply(result)
        except Exception as exc:
            LOG.exception("job failed session=%s", session_id)
            self._reply({"ok": False, "session_id": session_id, "error": str(exc)})

    def _reply(self, payload: dict) -> None:
        self.wfile.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.wfile.flush()


class _ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    # Serialize GPU work via _pipeline_lock; threading only for ping while busy.
    daemon_threads = True


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("CEREBEL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    LOG.info("preloading GEM-X models…")
    _get_pipeline()
    LOG.info("warm worker listening on %s:%s (models loaded, waiting for jobs)", HOST, PORT)
    with _ThreadedServer((HOST, PORT), _Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            LOG.info("warm worker stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
