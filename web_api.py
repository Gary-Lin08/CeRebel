#!/usr/bin/env python3
"""API-only backend for the Cerebel web app (webapp/ — Vite/React, deployed
separately, e.g. Cloudflare Pages).

Accepts an uploaded video over HTTP, turns it into a capture "session" in the
same on-disk shape the iOS gRPC capture path produces (data/sessions/<id>/),
and reuses the existing GemxPipelineQueue / IK pipeline to process it. Results
are read back from the real OpenSim output (opensim-motion.json,
estimated-dynamics.json) via motion_agent's deterministic tool layer — no
fabricated numbers, no LLM call on this path.

Stdlib-only (http.server), matching kinetic_lens_viewer.py's style. Run with
the server's .venv (grpc's venv has no extra deps needed here) or plain
python3 — this module only needs numpy indirectly via motion_agent, which is
only imported once a session actually reaches "ready".

Serves JSON + /media only — no static frontend files. Put this behind an
HTTPS tunnel (e.g. cloudflared, see ../cloudflared-config.yml) before pointing
a browser-hosted frontend at it; plain HTTP gets blocked as mixed content by
an HTTPS page.

    python3 web_api.py
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

LOG = logging.getLogger("cerebel.web_api")

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CEREBEL_DATA_DIR", str(ROOT / "data")))
HOST = os.environ.get("CEREBEL_WEB_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("CEREBEL_WEB_API_PORT", "8093"))
GEMX_ENABLED = os.environ.get("CEREBEL_GEMX_ENABLED", "1").lower() not in ("0", "false", "no")

MIN_DURATION_S = 3.0
MAX_DURATION_S = 20.0
TARGET_FPS = 30
MIN_JPEG_FRAMES = int(os.environ.get("CEREBEL_MIN_UPLOAD_FRAMES", "24"))

from gemx_pipeline import GemxPipelineQueue, GEMX_PYTHON  # noqa: E402

sys.path.insert(0, str(ROOT))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _ffprobe_duration(path: Path) -> float | None:
    try:
        proc = _run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            timeout=30,
        )
        return float(proc.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return None


def _ffprobe_dims(path: Path) -> tuple[int, int]:
    try:
        proc = _run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=s=x:p=0", str(path),
            ],
            timeout=30,
        )
        w, h = proc.stdout.strip().split("x")
        return int(w), int(h)
    except Exception:
        return 0, 0


def _extract_jpegs(video_path: Path, jpeg_dir: Path, fps: int) -> int:
    jpeg_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-vf", f"fps={fps}",
        "-start_number", "0",
        "-qscale:v", "3",
        str(jpeg_dir / "%06d.jpg"),
    ]
    _run(cmd, timeout=180)
    return sum(1 for _ in jpeg_dir.glob("*.jpg"))


def _extract_jpegs_background(
    session_id: str,
    video_path: Path,
    jpeg_dir: Path,
    fps: int,
) -> None:
    """Background JPEG extract overlapping GEM-X for web_api uploads."""
    try:
        n = _extract_jpegs(video_path, jpeg_dir, fps)
        LOG.info("background jpeg extract done session=%s frames=%s", session_id, n)
        summary_path = video_path.parent.parent / "summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session_id,
            "frames": n,
            "source": "web-api",
            "closed_at": _utc_now(),
            "jpegs_pending": False,
        }
        summary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if n < MIN_JPEG_FRAMES:
            LOG.warning(
                "background jpeg extract low frame count session=%s n=%s",
                session_id,
                n,
            )
    except Exception:
        LOG.exception("background jpeg extract failed session=%s", session_id)


class SessionError(Exception):
    def __init__(self, status: str, reason: str, suggestion: str | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.suggestion = suggestion


_QUEUE: GemxPipelineQueue | None = None
_QUEUE_LOCK = threading.Lock()
_EVENT_LOCK = threading.Lock()
_EVENT_FILE = DATA_DIR / "product_events.jsonl"
_ALLOWED_EVENTS = {
    "page_view",
    "upload_selected",
    "analysis_started",
    "analysis_ready",
    "analysis_error",
    "agent_question",
    "agent_answer",
}


def record_product_event(payload: dict) -> dict:
    event = str(payload.get("event") or "")[:64]
    if event not in _ALLOWED_EVENTS:
        raise SessionError("blocked", "Unsupported product event.")
    visitor_id = str(payload.get("visitor_id") or "anonymous")[:80]
    raw_properties = payload.get("properties")
    properties = raw_properties if isinstance(raw_properties, dict) else {}
    safe_properties = {
        str(key)[:64]: value
        for key, value in list(properties.items())[:20]
        if isinstance(value, (str, int, float, bool)) or value is None
    }
    row = {
        "received_at": _utc_now(),
        "event": event,
        "visitor_id": visitor_id,
        "properties": safe_properties,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _EVENT_LOCK:
        with _EVENT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"ok": True}


def _queue() -> GemxPipelineQueue:
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is None:
            _QUEUE = GemxPipelineQueue(DATA_DIR)
        return _QUEUE


def create_session(video_bytes: bytes, content_type: str, sport: str, movement: str) -> dict:
    ext = {
        "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
    }.get(content_type.split(";")[0].strip().lower(), ".mp4")

    tmp_dir = DATA_DIR / "_uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{uuid.uuid4()}{ext}"
    tmp_path.write_bytes(video_bytes)

    try:
        duration = _ffprobe_duration(tmp_path)
        if duration is None:
            raise SessionError(
                "blocked", "视频文件无法读取，可能已损坏或格式不受支持。",
                "请重新导出为 MP4/MOV/WebM 后再试一次。",
            )
        if duration < MIN_DURATION_S or duration > MAX_DURATION_S:
            raise SessionError(
                "blocked",
                f"视频长度为 {duration:.1f} 秒，不符合要求（建议 {MIN_DURATION_S:.0f}"
                f"–{MAX_DURATION_S:.0f} 秒）。",
                "请重新拍摄，确保完整包含起跳到落地的过程，同时控制在20秒以内。",
            )

        session_id = str(uuid.uuid4()).upper()
        session_dir = DATA_DIR / "sessions" / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        width, height = _ffprobe_dims(tmp_path)
        header = {
            "session_id": session_id,
            "device_model": "web-upload",
            "os_version": "",
            "app_version": "web-1.0",
            "width": width,
            "height": height,
            "target_fps": TARGET_FPS,
            "pixel_format": 3,
            "camera_position": 0,
            "synthetic_source": False,
            "sport": sport,
            "movement": movement,
            "source_duration_s": duration,
            "received_at": _utc_now(),
        }
        (session_dir / "header.json").write_text(
            json.dumps(header, indent=2) + "\n", encoding="utf-8"
        )

        # Keep source video on disk so GEM-X can start before JPEGs are ready.
        upload_dir = session_dir / "upload"
        upload_dir.mkdir(parents=True, exist_ok=True)
        source_path = upload_dir / f"source_video{ext}"
        shutil.copy2(tmp_path, source_path)

        est_frames = max(0, int(float(duration) * TARGET_FPS))
        if est_frames < MIN_JPEG_FRAMES:
            shutil.rmtree(session_dir, ignore_errors=True)
            raise SessionError(
                "blocked",
                f"视频过短，预计有效帧数不足（约 {est_frames} 帧）。",
                "请拍摄更完整的动作片段后再试。",
            )

        (session_dir / "summary.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "frames": est_frames,
                    "source": "web-api",
                    "closed_at": _utc_now(),
                    "jpegs_pending": True,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        limited_reason = None
        if width and height and min(width, height) < 480:
            limited_reason = "视频分辨率较低，部分细节可能不准确。"

        if not GEMX_ENABLED:
            status, error = "error", "GEM-X pipeline disabled on this server (CEREBEL_GEMX_ENABLED=0)."
        elif not Path(GEMX_PYTHON).is_file():
            status, error = (
                "error",
                f"GEM-X worker python not found at {GEMX_PYTHON}. "
                "This deployment has no GPU pipeline attached — connect a real "
                "GEM-X worker (CEREBEL_GEMX_ROOT/CEREBEL_GEMX_PYTHON) to process uploads.",
            )
        else:
            status, error = "queued", None

        if status == "queued":
            # Overlap: enqueue GEM-X on source video; extract JPEGs in background.
            threading.Thread(
                target=_extract_jpegs_background,
                args=(session_id, source_path, session_dir / "jpegs", TARGET_FPS),
                name=f"jpegs-{session_id[:8]}",
                daemon=True,
            ).start()
            _queue().enqueue(session_dir)
        else:
            # Still extract for debugging / partial assets when pipeline is offline.
            try:
                _extract_jpegs(source_path, session_dir / "jpegs", TARGET_FPS)
            except Exception:
                LOG.exception("jpeg extract failed for offline session %s", session_id)
            (session_dir / "gemx.json").write_text(
                json.dumps({"session_id": session_id, "ok": False, "error": error}, indent=2)
                + "\n",
                encoding="utf-8",
            )

        return {
            "status": "limited" if limited_reason else "ok",
            "session_id": session_id,
            "reason": limited_reason,
            "pipeline_status": status,
            "pipeline_error": error,
        }
    finally:
        tmp_path.unlink(missing_ok=True)


def session_status(session_id: str) -> dict:
    session_dir = DATA_DIR / "sessions" / session_id
    if not session_dir.is_dir():
        return {"stage": "not_found"}

    ik_json = session_dir / "ik.json"
    gemx_json = session_dir / "gemx.json"

    if ik_json.is_file():
        return {"stage": "ready"}

    if gemx_json.is_file():
        manifest = json.loads(gemx_json.read_text(encoding="utf-8"))
        if manifest.get("ok") is False:
            return {"stage": "error", "error": manifest.get("error") or "GEM-X pipeline failed."}
        if manifest.get("ik_error"):
            return {"stage": "error", "error": manifest["ik_error"]}
        if manifest.get("opensim_motion"):
            return {"stage": "biomechanics_ik"}
        return {"stage": "pose_estimation"}

    # gemx.json is only written once the whole pipeline finishes (success or
    # not) — while a job is actually running, GemxPipelineQueue instead keeps
    # data/latest_mesh.json updated for whichever session it's currently on.
    # Use that for real intermediate progress instead of a flat "uploading".
    pointer = DATA_DIR / "latest_mesh.json"
    if pointer.is_file():
        try:
            ptr = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ptr = {}
        if ptr.get("session_id") == session_id:
            if ptr.get("status") == "error":
                return {"stage": "error", "error": ptr.get("error") or "GEM-X pipeline failed."}
            if ptr.get("ik_status") == "running":
                return {"stage": "biomechanics_ik"}
            if ptr.get("status") == "running":
                return {"stage": "pose_estimation"}

    return {"stage": "uploading"}


def session_results(session_id: str) -> dict:
    session_dir = DATA_DIR / "sessions" / session_id
    ik_json = session_dir / "ik.json"
    if not ik_json.is_file():
        raise SessionError("not_ready", "Session is not ready yet.")

    from motion_agent.motion.sequence import load_motion_sequence
    from motion_agent.tools.motion_tools import build_tool_map

    manifest = json.loads(ik_json.read_text(encoding="utf-8"))
    header = {}
    header_path = session_dir / "header.json"
    if header_path.is_file():
        header = json.loads(header_path.read_text(encoding="utf-8"))

    seq = load_motion_sequence(session_dir)
    tools = build_tool_map(seq)
    duration = seq.duration_s

    # Takeoff / landing from real contact-phase transitions (ground <-> flight).
    key_moments = []
    phases = seq.contact_phase
    for i in range(1, len(phases)):
        if phases[i - 1] != "flight" and phases[i] == "flight" and not any(
            k["label"] == "Takeoff" for k in key_moments
        ):
            key_moments.append({"label": "Takeoff", "time": float(seq.timestamps[i])})
        if phases[i - 1] == "flight" and phases[i] != "flight":
            key_moments = [k for k in key_moments if k["label"] != "Landing"]
            key_moments.append({"label": "Landing", "time": float(seq.timestamps[i])})

    knee = tools["get_joint_trajectory"](
        joint="right_knee", start_time=0, end_time=duration,
        quantities=["angle", "angular_velocity"], max_points=60,
    )
    hip = tools["get_joint_trajectory"](
        joint="right_hip", start_time=0, end_time=duration,
        quantities=["angle", "angular_velocity"], max_points=60,
    )
    com = tools["get_com_trajectory"](start_time=0, end_time=duration, max_points=60)

    metrics = []
    if not knee.get("error"):
        metrics.append({
            "key": "knee", "label": "Knee flexion", "unit": "°",
            "value": knee["statistics"].get("max_angle_deg", 0.0),
            "confidence": "Reconstructed",
        })
        metrics.append({
            "key": "kneeVel", "label": "Knee angular velocity", "unit": "°/s",
            "value": abs(knee["statistics"].get("peak_angular_velocity_deg_s", 0.0)),
            "confidence": "Reconstructed",
        })
    if not hip.get("error"):
        metrics.append({
            "key": "hip", "label": "Hip rotation speed", "unit": "°/s",
            "value": abs(hip["statistics"].get("peak_angular_velocity_deg_s", 0.0)),
            "confidence": "Reconstructed",
        })

    # Net X-axis (OpenSim ground-plane) displacement of the pelvis/CoM across
    # the whole clip. For a fixed-takeoff trick this reads as "lateral drift
    # on landing"; for a locomotion clip (running, skating) the same number
    # is mostly real travel distance, not drift — so the label/finding below
    # stay generic rather than assuming a jump-and-land movement.
    com_horizontal_cm = 0.0
    if not com.get("error"):
        xs = [row[0] for row in com["position_m"]["values"]]
        if xs:
            com_horizontal_cm = (xs[-1] - xs[0]) * 100.0
        metrics.append({
            "key": "com", "label": "Center-of-mass horizontal displacement", "unit": " cm",
            "value": abs(com_horizontal_cm), "confidence": "Estimated",
        })

    finding = (
        f"Center of mass moves roughly {abs(com_horizontal_cm):.1f} cm horizontally "
        f"(ground-plane X axis) over the course of the clip — for a fixed-takeoff "
        f"trick this is landing drift; for a locomotion movement it's mostly travel distance."
        if com_horizontal_cm else
        "No significant horizontal center-of-mass movement was detected in this clip."
    )

    limitation_bits = [
        "Estimated from one monocular camera view (GEM-X SOMA77 → OpenSim Rajagopal retarget)."
    ]
    if not seq.has_dynamics():
        limitation_bits.append("No ground-reaction-force dynamics available for this session.")
    alignment = manifest.get("alignment") or {}
    overall_mean_m = alignment.get("overall_mean_m") if isinstance(alignment, dict) else None
    if overall_mean_m is not None:
        limitation_bits.append(f"IK retarget mean joint alignment error: {overall_mean_m:.2f} m.")

    video_rel = "session.mp4" if (session_dir / "ik" / "session.mp4").is_file() else None

    return {
        "session_id": session_id,
        "sport": header.get("sport"),
        "movement": header.get("movement"),
        "duration": duration,
        "fps": seq.fps,
        "video_url": f"/media/{session_id}/{video_rel}" if video_rel else None,
        "motion_data_url": f"/media/{session_id}/motion-data.json",
        "opensim_motion_url": f"/media/{session_id}/opensim-motion.json",
        "estimated_dynamics_url": f"/media/{session_id}/estimated-dynamics.json"
        if (session_dir / "ik" / "estimated-dynamics.json").is_file()
        else None,
        "key_moments": key_moments,
        "metrics": metrics,
        "finding": finding,
        "limitation": " ".join(limitation_bits),
    }


def list_sessions() -> list[dict]:
    root = DATA_DIR / "sessions"
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir() or not (d / "header.json").is_file():
            continue
        header = json.loads((d / "header.json").read_text(encoding="utf-8"))
        st = session_status(d.name)
        out.append({
            "session_id": d.name,
            "sport": header.get("sport"),
            "movement": header.get("movement") or "Untitled movement",
            "received_at": header.get("received_at"),
            "stage": st["stage"],
        })
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "CerebelWebAPI/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj: object, status: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: A003
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: A003
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        parts = [p for p in path.split("/") if p]

        try:
            if parts == ["api", "sessions"]:
                self._json({"sessions": list_sessions()})
                return
            if parts == ["api", "health"]:
                self._json({
                    "ok": True,
                    "pipeline_available": bool(GEMX_ENABLED and Path(GEMX_PYTHON).is_file()),
                })
                return
            if len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "status":
                self._json(session_status(parts[2]))
                return
            if len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "results":
                try:
                    self._json(session_results(parts[2]))
                except SessionError as exc:
                    self._json({"error": exc.reason}, 409)
                return
            if len(parts) >= 3 and parts[0] == "media":
                session_id, name = parts[1], "/".join(parts[2:])
                self._file(DATA_DIR / "sessions" / session_id / "ik" / name)
                return
        except Exception as exc:  # noqa: BLE001
            LOG.exception("GET %s failed", path)
            self._json({"error": str(exc)}, 500)
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: A003
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        parts = [p for p in path.split("/") if p]
        qs = parse_qs(parsed.query)

        if parts == ["api", "events"]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16_384:
                self._json({"error": "invalid event body"}, 400)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self._json(record_product_event(payload))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json({"error": "invalid JSON"}, 400)
            except SessionError as exc:
                self._json({"error": exc.reason}, 400)
            return

        if parts != ["api", "sessions"]:
            self._json({"error": "not found"}, 404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self._json({"error": "empty body"}, 400)
            return
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "video/mp4")
        sport = (qs.get("sport") or ["Snowboarding"])[0]
        movement = (qs.get("movement") or [""])[0]

        try:
            result = create_session(body, content_type, sport, movement)
            self._json(result)
        except SessionError as exc:
            self._json({"status": exc.status, "reason": exc.reason, "suggestion": exc.suggestion})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("upload failed")
            self._json({"error": str(exc)}, 500)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("CEREBEL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if GEMX_ENABLED and Path(GEMX_PYTHON).is_file():
        _queue()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    LOG.info("Cerebel web API on http://%s:%s (data=%s)", HOST, PORT, DATA_DIR)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
