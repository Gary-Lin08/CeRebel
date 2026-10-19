#!/usr/bin/env python3
"""HTTP server for Kinetic Lens OpenSim visualization of CeRebel sessions.

Watches ``$CEREBEL_DATA_DIR/latest_mesh.json`` and serves the Kinetic Lens UI
with the newest session that has completed OpenSim IK.

When ``CEREBEL_FAKE_EGOCENTRIC=1`` (or ``--fake-egocentric``), the raw source
video is not exposed and the UI is instructed to show the GEM-X SOMA mesh pack
instead.

    /root/CerebelServer/.venv/bin/python kinetic_lens_viewer.py [--fake-egocentric]
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from cerebel_config import apply_cli_flags, fake_egocentric_enabled

LOG = logging.getLogger("cerebel.kinetic_lens")

DATA_DIR = Path(os.environ.get("CEREBEL_DATA_DIR", "/root/CerebelServer/data"))
POINTER = DATA_DIR / "latest_mesh.json"
KINETIC_LENS_ROOT = Path(
    os.environ.get("CEREBEL_KINETIC_LENS_ROOT", str(Path(__file__).resolve().parent / "kinetic-lens"))
)
DIST = KINETIC_LENS_ROOT / "dist"
HOST = os.environ.get("CEREBEL_KINETIC_LENS_HOST", "0.0.0.0")
PORT = int(os.environ.get("CEREBEL_KINETIC_LENS_PORT", "8091"))
POLL_S = float(os.environ.get("CEREBEL_KINETIC_LENS_POLL_S", "2.0"))

# Session assets cached from latest_mesh.json
_session: dict | None = None


def _load_pointer() -> dict | None:
    if not POINTER.is_file():
        return None
    try:
        return json.loads(POINTER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("bad latest_mesh.json: %s", exc)
        return None


def _soma_paths(session_dir: Path) -> tuple[Path, Path]:
    display = session_dir / "display"
    return display / "soma-mesh.bin", display / "soma-mesh.json"


def _resolve_session(ptr: dict | None) -> dict | None:
    if not ptr or ptr.get("status") != "ready":
        return None
    session_id = ptr.get("session_id")
    if not session_id:
        return None
    session_dir = DATA_DIR / "sessions" / session_id
    soma_bin, soma_meta = _soma_paths(session_dir)

    ik_motion = ptr.get("opensim_motion")
    if ik_motion and Path(ik_motion).is_file():
        ik_dir = session_dir / "ik"
        return {
            "session_id": session_id,
            "fps": float(ptr.get("fps") or 30.0),
            "ik_dir": ik_dir,
            "motion_data": ik_dir / "motion-data.json",
            "opensim_motion": Path(ik_motion),
            "estimated_dynamics": ik_dir / "estimated-dynamics.json",
            "glb": ik_dir / "model" / "rajagopal-bones.glb",
            "video": ik_dir / "session.mp4",
            "poster": ik_dir / "poster.jpg",
            "soma_mesh_bin": soma_bin,
            "soma_mesh_meta": soma_meta,
            "hpe_results": Path(ptr["hpe_results"]) if ptr.get("hpe_results") else None,
        }
    # Fallback: check session ik.json
    ik_json = session_dir / "ik.json"
    if ik_json.is_file():
        manifest = json.loads(ik_json.read_text(encoding="utf-8"))
        ik_dir = session_dir / "ik"
        return {
            "session_id": session_id,
            "fps": float(manifest.get("fps") or ptr.get("fps") or 30.0),
            "ik_dir": ik_dir,
            "motion_data": Path(manifest.get("motion_data", ik_dir / "motion-data.json")),
            "opensim_motion": Path(manifest.get("opensim_motion", ik_dir / "opensim-motion.json")),
            "estimated_dynamics": Path(
                manifest.get("estimated_dynamics", ik_dir / "estimated-dynamics.json")
            ),
            "glb": Path(manifest.get("glb", ik_dir / "model" / "rajagopal-bones.glb")),
            "video": Path(manifest.get("video", ik_dir / "session.mp4")),
            "poster": ik_dir / "poster.jpg",
            "soma_mesh_bin": soma_bin,
            "soma_mesh_meta": soma_meta,
            "hpe_results": Path(ptr["hpe_results"]) if ptr.get("hpe_results") else None,
        }
    return None


def _watch_loop() -> None:
    global _session
    last_key = None
    while True:
        ptr = _load_pointer()
        sess = _resolve_session(ptr)
        if sess:
            key = f"{sess['session_id']}:{sess['opensim_motion'].stat().st_mtime_ns}"
            if key != last_key:
                LOG.info(
                    "serving Kinetic Lens for session %s (fake_egocentric=%s)",
                    sess["session_id"],
                    fake_egocentric_enabled(),
                )
                _session = sess
                last_key = key
        elif ptr and ptr.get("status") == "running":
            LOG.debug("waiting for IK on session %s", ptr.get("session_id"))
        time.sleep(POLL_S)


# Asset path aliases used by the Kinetic Lens frontend
_ASSET_ALIASES = {
    "/assets/layup-motion-data.json": "motion_data",
    "/assets/model/opensim-motion.json": "opensim_motion",
    "/assets/model/estimated-dynamics.json": "estimated_dynamics",
    "/assets/model/rajagopal-bones.glb": "glb",
    "/assets/layup-original.mp4": "video",
    "/assets/layup-original-poster.jpg": "poster",
    "/assets/soma-mesh.bin": "soma_mesh_bin",
    "/assets/soma-mesh.json": "soma_mesh_meta",
}


class KineticLensHandler(BaseHTTPRequestHandler):
    server_version = "CerebelKineticLens/1.0"

    def log_message(self, fmt: str, *args) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send_bytes(self, data: bytes, content_type: str, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, *, content_type: str | None = None) -> None:
        if not path.is_file():
            self._send_bytes(b"not found", "text/plain", status=404)
            return
        data = path.read_bytes()
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self._send_bytes(data, ctype)

    def _send_json(self, obj: object) -> None:
        self._send_bytes(json.dumps(obj, indent=2).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        fake_ego = fake_egocentric_enabled()

        if path == "/api/session":
            if _session:
                soma_bin = _session.get("soma_mesh_bin")
                self._send_json(
                    {
                        "session_id": _session["session_id"],
                        "fps": _session["fps"],
                        "ready": True,
                        "fake_egocentric": fake_ego,
                        "soma_mesh_ready": bool(
                            soma_bin and Path(soma_bin).is_file()
                        ),
                        "hide_source_video": fake_ego,
                    }
                )
            else:
                self._send_json(
                    {
                        "ready": False,
                        "status": "waiting for IK results",
                        "fake_egocentric": fake_ego,
                    }
                )
            return

        if path in _ASSET_ALIASES and _session:
            key = _ASSET_ALIASES[path]
            # In fake-egocentric mode never expose the raw third-person capture.
            if fake_ego and key in ("video", "poster"):
                self._send_bytes(b"hidden in fake-egocentric mode", "text/plain", status=404)
                return
            target = _session.get(key)
            if target and Path(target).is_file():
                ctype = None
                if key == "soma_mesh_bin":
                    ctype = "application/octet-stream"
                self._send_file(Path(target), content_type=ctype)
                return
            # Fall back to bundled demo assets for missing session outputs
            # (never for video/poster when fake-ego — that would leak demo footage as "source")
            if not (fake_ego and key in ("video", "poster")):
                fallback = DIST / path.lstrip("/")
                if fallback.is_file():
                    self._send_file(fallback)
                    return
            self._send_bytes(b"asset not ready", "text/plain", status=404)
            return

        # Static files from kinetic-lens dist
        if path in ("", "/"):
            path = "/index.html"
        static = DIST / path.lstrip("/")
        if static.is_file() and static.resolve().is_relative_to(DIST.resolve()):
            self._send_file(static)
            return

        self._send_bytes(b"not found", "text/plain", status=404)


def main(argv: list[str] | None = None) -> int:
    remaining = apply_cli_flags(argv if argv is not None else sys.argv[1:])
    if remaining:
        LOG.error("unknown arguments: %s", " ".join(remaining))
        return 2

    logging.basicConfig(
        level=os.environ.get("CEREBEL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not DIST.is_dir():
        LOG.error("kinetic-lens dist/ not found — run: cd kinetic-lens && npm install && npm run build")
        return 1

    import threading

    threading.Thread(target=_watch_loop, name="kinetic-lens-watch", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), KineticLensHandler)
    LOG.info(
        "Kinetic Lens viewer on http://%s:%s (watching %s, fake_egocentric=%s)",
        HOST,
        PORT,
        POINTER,
        fake_egocentric_enabled(),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
