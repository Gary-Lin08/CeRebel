#!/usr/bin/env python3
"""CeRebel web video upload + pipeline progress (port 8093).

Accepts a video file, prepares a session, enqueues the internal motion pipeline,
and exposes progress for the UI.
"""

from __future__ import annotations

import cgi
import json
import logging
import math
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from gemx_pipeline import GemxPipelineQueue, PipelineResult

LOG = logging.getLogger("cerebel.upload")

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CEREBEL_DATA_DIR", str(ROOT / "data")))
UI_DIR = Path(os.environ.get("CEREBEL_UPLOAD_UI", str(ROOT / "upload_ui")))
HOST = os.environ.get("CEREBEL_UPLOAD_HOST", "0.0.0.0")
PORT = int(os.environ.get("CEREBEL_UPLOAD_PORT", "8093"))
MAX_UPLOAD_BYTES = int(os.environ.get("CEREBEL_UPLOAD_MAX_BYTES", str(512 * 1024 * 1024)))
TARGET_FPS = float(os.environ.get("CEREBEL_UPLOAD_FPS", "30"))
CHAT_PORT = int(os.environ.get("CEREBEL_CHAT_PORT", "8092"))
KINETIC_LENS_PORT = int(os.environ.get("CEREBEL_KINETIC_LENS_PORT", "8091"))
KINETIC_LENS_URL = os.environ.get(
    "CEREBEL_KINETIC_LENS_URL",
    "https://0i5-79nucyondu7h6sp1a-cdhea2bz-custom.service.onethingrobot.com/",
)
CHAT_URL = os.environ.get(
    "CEREBEL_CHAT_URL",
    "https://0i5-79nucyondu7h6sp1a-cdhea2bq-custom.service.onethingrobot.com/",
)
WEBSITE_URL = os.environ.get("CEREBEL_WEBSITE_URL", "https://cerebel.tech/")

_QUEUE: GemxPipelineQueue | None = None
_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}

# Shown when the GPU worker could not be reached (failed_at == "gpu_unavailable").
GPU_UNAVAILABLE_MESSAGE = (
    "Our analysis server was offline, so this swing wasn't analyzed and it didn't use one of "
    "your analyses. Please upload it again in a few minutes."
)

STAGES = (
    "uploading",
    "extracting",
    "queued",
    "gemx",
    "ik",
    "ready",
    "error",
)

STAGE_PERCENT = {
    "uploading": 5,
    "extracting": 15,
    "queued": 22,
    "gemx": 45,
    "ik": 78,
    "ready": 100,
    "error": 100,
}

# Soft upper caps while a long stage is still running (never claim 100 early).
STAGE_CREEP_CEILING = {
    "gemx": 74,
    "ik": 96,
}
# Time constants (seconds) for 1 - exp(-t/tau) creep within a stage.
STAGE_CREEP_TAU_S = {
    "gemx": 55.0,
    "ik": 35.0,
}


def _stage_creep_percent(stage: str, prog: dict | None) -> float:
    """Advance percent within Foundation/Analysis while work is in flight.

    GEM-X / IK do not emit fine-grained progress; this maps elapsed time in the
    stage to a smooth asymptotic climb so the upload UI bar keeps moving.
    """
    lo = float(STAGE_PERCENT.get(stage, 0))
    hi = float(STAGE_CREEP_CEILING.get(stage, lo))
    if hi <= lo:
        return lo
    prog = prog or {}
    started = prog.get("stage_started_at")
    try:
        started_f = float(started) if started is not None else None
    except (TypeError, ValueError):
        started_f = None
    # Reset the clock when entering a new stage.
    if prog.get("stage") != stage or started_f is None:
        started_f = time.time()
    tau = float(STAGE_CREEP_TAU_S.get(stage, 45.0))
    elapsed = max(0.0, time.time() - started_f)
    t = 1.0 - math.exp(-elapsed / max(tau, 1e-3))
    pct = lo + (hi - lo) * t
    prev = prog.get("percent")
    try:
        prev_f = float(prev) if prev is not None else lo
    except (TypeError, ValueError):
        prev_f = lo
    # Monotonic within the stage; never jump backwards on poll jitter.
    return round(min(hi, max(lo, prev_f, pct)), 1)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _progress_path(session_dir: Path) -> Path:
    return session_dir / "upload_progress.json"


def _write_progress(session_id: str, **fields) -> dict:
    session_dir = DATA_DIR / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = _progress_path(session_dir)
    with _LOCK:
        current = dict(_JOBS.get(session_id) or {})
        if path.is_file() and not current:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = {}
        prev_stage = current.get("stage")
        current.update(fields)
        current["session_id"] = session_id
        stage = current.get("stage") or "uploading"
        if stage != prev_stage or current.get("stage_started_at") is None:
            # Fresh clock whenever the pipeline stage changes.
            if "stage_started_at" not in fields or stage != prev_stage:
                current["stage_started_at"] = time.time()
        if "percent" not in fields:
            if stage in STAGE_CREEP_CEILING:
                current["percent"] = _stage_creep_percent(stage, current)
            else:
                current["percent"] = STAGE_PERCENT.get(stage, current.get("percent", 0))
        current["updated_at"] = time.time()
        current.setdefault("created_at", _now())
        current["ports"] = {
            "kinetic_lens": KINETIC_LENS_PORT,
            "chat": CHAT_PORT,
            "upload": PORT,
        }
        current["links"] = {
            "kinetic_lens": KINETIC_LENS_URL,
            "chat": CHAT_URL,
            "website": WEBSITE_URL,
        }
        _JOBS[session_id] = current
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return current


def _read_progress(session_id: str) -> dict | None:
    with _LOCK:
        if session_id in _JOBS:
            return dict(_JOBS[session_id])
    path = _progress_path(DATA_DIR / "sessions" / session_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _probe_video(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,avg_frame_rate,duration",
        "-of",
        "json",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffprobe failed")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or [{}]
    stream = streams[0] if streams else {}
    fps = TARGET_FPS
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key) or ""
        if isinstance(raw, str) and "/" in raw:
            num, den = raw.split("/", 1)
            try:
                val = float(num) / float(den)
                if val > 0:
                    fps = val
                    break
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    duration = float(stream.get("duration") or 0.0)
    return {"width": width, "height": height, "fps": fps, "duration": duration}


def _extract_frames(session_id: str, video_path: Path, jpeg_dir: Path, fps: float) -> int:
    """Write session JPEGs (same fps filter GEM-X uses for video loads).

    Safe to run in parallel with GEM-X: does not downgrade progress past
    extracting/queued once the pipeline has advanced.
    """
    jpeg_dir.mkdir(parents=True, exist_ok=True)
    for old in jpeg_dir.glob("*.jpg"):
        old.unlink()
    prog = _read_progress(session_id) or {}
    stage = prog.get("stage") or "uploading"
    if stage in ("uploading", "extracting"):
        _write_progress(
            session_id,
            stage="extracting",
            percent=12,
            message="Preparing motion frames…",
        )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"fps={fps}",
        "-q:v",
        "2",
        str(jpeg_dir / "%06d.jpg"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "frame prepare failed").strip()[-1500:]
        raise RuntimeError(detail)
    frames = sorted(jpeg_dir.glob("*.jpg"))
    if len(frames) < 8:
        raise RuntimeError(f"need ≥8 frames, extracted {len(frames)}")
    prog = _read_progress(session_id) or {}
    stage = prog.get("stage") or "extracting"
    fields: dict = {
        "frame_count": len(frames),
        "jpegs_ready": True,
        "message": f"Prepared {len(frames)} frames for playback / RGB tools",
    }
    if stage in ("uploading", "extracting"):
        fields["stage"] = "extracting"
        fields["percent"] = 20
    _write_progress(session_id, **fields)
    return len(frames)


def _extract_frames_background(
    session_id: str,
    video_path: Path,
    session_dir: Path,
    fps: float,
    original_name: str,
) -> None:
    """Background JPEG extract for web uploads (overlaps GEM-X)."""
    try:
        frame_count = _extract_frames(session_id, video_path, session_dir / "jpegs", fps)
        summary = {
            "session_id": session_id,
            "frames": frame_count,
            "source": "web-upload",
            "filename": original_name,
            "closed_at": _now(),
        }
        (session_dir / "summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        LOG.info(
            "background jpeg extract done session=%s frames=%s",
            session_id,
            frame_count,
        )
    except Exception:
        LOG.exception("background jpeg extract failed session=%s", session_id)
        prog = _read_progress(session_id) or {}
        # Do not fail the whole job if GEM-X already advanced; mark for UI/logs.
        if prog.get("stage") in (None, "uploading", "extracting", "queued"):
            _write_progress(
                session_id,
                stage="error",
                percent=100,
                message="Frame prepare failed",
                error="Frame prepare failed",
            )
        else:
            _write_progress(session_id, jpegs_ready=False, jpegs_error=True)


def _watch_pipeline(session_id: str) -> None:
    """Mirror latest_mesh.json into upload_progress while this session is active."""
    pointer = DATA_DIR / "latest_mesh.json"
    deadline = time.time() + 7200
    while time.time() < deadline:
        prog = _read_progress(session_id) or {}
        stage = prog.get("stage")
        if stage in ("ready", "error"):
            return
        if pointer.is_file():
            try:
                ptr = json.loads(pointer.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                ptr = {}
            if ptr.get("session_id") == session_id:
                status = ptr.get("status") or ""
                ik_status = ptr.get("ik_status") or ""
                err = ptr.get("error")
                if status == "error":
                    failed_at = ptr.get("failed_at")
                    _write_progress(
                        session_id,
                        stage="error",
                        percent=100,
                        message=(
                            GPU_UNAVAILABLE_MESSAGE if failed_at == "gpu_unavailable" else err or "Pipeline failed"
                        ),
                        error=err or "Pipeline failed",
                        failed_at=failed_at,
                    )
                    return
                if status == "ready" and ik_status in ("ready", "skipped", "error", ""):
                    if ik_status == "error":
                        _write_progress(
                            session_id,
                            stage="error",
                            percent=100,
                            message="Biomechanical analysis did not complete",
                            error="Analysis failed",
                            ik_status=ik_status,
                            failed_at="ik",
                        )
                    else:
                        _write_progress(
                            session_id,
                            stage="ready",
                            percent=100,
                            message="Processing complete",
                            ik_status=ik_status or "ready",
                            opensim_motion=ptr.get("opensim_motion") or "",
                        )
                    return
                if status == "running" and ik_status == "running":
                    _write_progress(
                        session_id,
                        stage="ik",
                        percent=_stage_creep_percent("ik", prog),
                        message="CeRebel Foundation is refining biomechanical insight…",
                        ik_status="running",
                    )
                elif status == "running":
                    _write_progress(
                        session_id,
                        stage="gemx",
                        percent=_stage_creep_percent("gemx", prog),
                        message="CeRebel Foundation is building motion understanding…",
                    )
        time.sleep(1.5)


def _on_pipeline_complete(result: PipelineResult) -> None:
    session_id = result.session_id
    if result.ok and result.ik_ok:
        _write_progress(
            session_id,
            stage="ready",
            percent=100,
            message="Processing complete",
            ik_status="ready",
            opensim_motion=str(result.opensim_motion or ""),
            error=None,
        )
    elif result.ok:
        _write_progress(
            session_id,
            stage="ready" if not result.ik_error else "error",
            percent=100,
            message=(
                "Motion insight ready"
                if not result.ik_error
                else f"Analysis incomplete: details are not available"
            ),
            ik_status="skipped" if not result.ik_error else "error",
            error=result.ik_error,
            opensim_motion=str(result.opensim_motion or ""),
            failed_at=result.failed_at,
        )
    else:
        _write_progress(
            session_id,
            stage="error",
            percent=100,
            message=(
                GPU_UNAVAILABLE_MESSAGE
                if result.failed_at == "gpu_unavailable"
                else result.error or "Pipeline failed"
            ),
            error=result.error or "Pipeline failed",
            failed_at=result.failed_at,
        )


def ensure_queue() -> GemxPipelineQueue:
    """Create the GEM-X queue on first use.

    main() creates it for the standalone upload server; the consumer app
    (motion_agent chat_server) imports this module without calling main(),
    so it needs the queue created lazily here.
    """
    global _QUEUE
    with _LOCK:
        if _QUEUE is None:
            _QUEUE = GemxPipelineQueue(DATA_DIR, on_complete=_on_pipeline_complete, wait_for_warm=True)
        return _QUEUE


def _process_uploaded_session(session_id: str, original_name: str) -> None:
    session_dir = DATA_DIR / "sessions" / session_id
    video_path = session_dir / "upload" / "source_video"
    # Resolve extension from saved name
    candidates = list((session_dir / "upload").glob("source_video.*"))
    if candidates:
        video_path = candidates[0]
    try:
        meta = _probe_video(video_path)
        fps = float(meta["fps"] or TARGET_FPS)
        if fps > 60:
            fps = TARGET_FPS
        width = meta["width"]
        height = meta["height"]
        header = {
            "session_id": session_id,
            "device_model": "web-upload",
            "os_version": "",
            "app_version": "upload-0.1",
            "width": width,
            "height": height,
            "target_fps": fps,
            "pixel_format": 3,
            "camera_position": 0,
            "synthetic_source": False,
            "source_filename": original_name,
            "intrinsics": {"fx": 0.0, "fy": 0.0, "cx": 0.0, "cy": 0.0},
            "received_at": _now(),
        }
        (session_dir / "header.json").write_text(
            json.dumps(header, indent=2) + "\n", encoding="utf-8"
        )
        # Enqueue GEM-X immediately on the source video; JPEG extract overlaps
        # in the background (same fps filter) for IK stitch / RGB tools.
        est_frames = max(0, int(float(meta.get("duration") or 0.0) * fps))
        _write_progress(
            session_id,
            stage="queued",
            percent=22,
            message="Queued for CeRebel Foundation (frames preparing in parallel)…",
            fps=fps,
            width=width,
            height=height,
            duration_s=meta.get("duration") or 0,
            filename=original_name,
            frame_count=est_frames,
            jpegs_ready=False,
        )
        (session_dir / "summary.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "frames": est_frames,
                    "source": "web-upload",
                    "filename": original_name,
                    "closed_at": _now(),
                    "jpegs_pending": True,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        queue = ensure_queue()
        threading.Thread(
            target=_watch_pipeline, args=(session_id,), name=f"watch-{session_id[:8]}", daemon=True
        ).start()
        threading.Thread(
            target=_extract_frames_background,
            args=(session_id, video_path, session_dir, fps, original_name),
            name=f"jpegs-{session_id[:8]}",
            daemon=True,
        ).start()
        queue.enqueue(session_dir)
    except Exception as exc:
        LOG.exception("upload processing failed session=%s", session_id)
        _write_progress(
            session_id,
            stage="error",
            percent=100,
            message=str(exc),
            error=str(exc),
        )


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def _safe_filename(name: str) -> str:
    base = Path(name or "video.mp4").name
    base = re.sub(r"[^\w.\-]+", "_", base)
    return base[:180] or "video.mp4"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        LOG.info("%s - " + fmt, self.address_string(), *args)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._serve_file(UI_DIR / "index.html")
        if path.startswith("/static/"):
            # Resolve first: "/static/../../.env.product" must never leave the UI folder.
            ui_root = UI_DIR.resolve()
            target = (ui_root / path[len("/static/") :]).resolve()
            if ui_root not in target.parents:
                return _json_response(self, 404, {"error": True, "message": "not found"})
            return self._serve_file(target)
        if path == "/api/health":
            return _json_response(
                self,
                200,
                {
                    "ok": True,
                    "port": PORT,
                    "queue": _QUEUE is not None,
                    "max_upload_bytes": MAX_UPLOAD_BYTES,
                },
            )
        if path == "/api/progress":
            qs = parse_qs(parsed.query)
            session_id = (qs.get("session_id") or [""])[0].strip()
            if not session_id:
                return _json_response(self, 400, {"error": True, "message": "session_id required"})
            prog = _read_progress(session_id)
            if not prog:
                return _json_response(self, 404, {"error": True, "message": "unknown session"})
            # Refresh from pointer if still in flight
            if prog.get("stage") not in ("ready", "error"):
                pointer = DATA_DIR / "latest_mesh.json"
                if pointer.is_file():
                    try:
                        ptr = json.loads(pointer.read_text(encoding="utf-8"))
                        if ptr.get("session_id") == session_id:
                            prog = _enrich_from_pointer(session_id, prog, ptr)
                    except (OSError, json.JSONDecodeError):
                        pass
            return _json_response(self, 200, prog)
        _json_response(self, 404, {"error": True, "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/upload":
            return _json_response(self, 404, {"error": True, "message": "not found"})
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return _json_response(self, 400, {"error": True, "message": "empty body"})
        if length > MAX_UPLOAD_BYTES:
            return _json_response(
                self,
                413,
                {
                    "error": True,
                    "message": f"file too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
                },
            )
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return _json_response(
                self, 400, {"error": True, "message": "expected multipart/form-data"}
            )

        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": ctype,
            "CONTENT_LENGTH": str(length),
        }
        try:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ=environ,
                keep_blank_values=True,
            )
        except Exception as exc:
            LOG.exception("multipart parse failed")
            return _json_response(self, 400, {"error": True, "message": f"bad multipart: {exc}"})

        fileitem = form["file"] if "file" in form else None
        if fileitem is None:
            # tolerate alternate field names
            for key in form.keys():
                item = form[key]
                if getattr(item, "filename", None):
                    fileitem = item
                    break
        if fileitem is None or not getattr(fileitem, "file", None):
            return _json_response(self, 400, {"error": True, "message": "missing file field"})

        original = _safe_filename(getattr(fileitem, "filename", None) or "video.mp4")
        ext = Path(original).suffix.lower() or ".mp4"
        if ext not in {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".mpeg", ".mpg"}:
            return _json_response(
                self,
                400,
                {"error": True, "message": f"unsupported video type: {ext}"},
            )

        session_id = str(uuid.uuid4()).upper()
        session_dir = DATA_DIR / "sessions" / session_id
        upload_dir = session_dir / "upload"
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / f"source_video{ext}"
        _write_progress(
            session_id,
            stage="uploading",
            percent=8,
            message="Receiving upload…",
            filename=original,
        )
        try:
            with dest.open("wb") as out:
                shutil.copyfileobj(fileitem.file, out, length=1024 * 1024)
        except Exception as exc:
            LOG.exception("failed to save upload")
            _write_progress(
                session_id,
                stage="error",
                percent=100,
                message=str(exc),
                error=str(exc),
            )
            return _json_response(self, 500, {"error": True, "message": str(exc), "session_id": session_id})

        size = dest.stat().st_size
        if size < 1024:
            _write_progress(
                session_id,
                stage="error",
                percent=100,
                message="Uploaded file is too small",
                error="Uploaded file is too small",
            )
            return _json_response(self, 400, {"error": True, "message": "file too small", "session_id": session_id})

        _write_progress(
            session_id,
            stage="extracting",
            percent=10,
            message="Upload saved — preparing for CeRebel Foundation…",
            filename=original,
            bytes=size,
        )
        threading.Thread(
            target=_process_uploaded_session,
            args=(session_id, original),
            name=f"upload-{session_id[:8]}",
            daemon=True,
        ).start()
        return _json_response(
            self,
            200,
            {
                "ok": True,
                "session_id": session_id,
                "filename": original,
                "bytes": size,
                "progress_url": f"/api/progress?session_id={session_id}",
            },
        )

    def _serve_file(self, path: Path) -> None:
        if not path.is_file():
            return _json_response(self, 404, {"error": True, "message": f"missing {path.name}"})
        data = path.read_bytes()
        mime, _ = mimetypes.guess_type(str(path))
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


def _enrich_from_pointer(session_id: str, prog: dict, ptr: dict) -> dict:
    status = ptr.get("status") or ""
    ik_status = ptr.get("ik_status") or ""
    if status == "error":
        return _write_progress(
            session_id,
            stage="error",
            message=ptr.get("error") or "Pipeline failed",
            error=ptr.get("error") or "Pipeline failed",
        )
    if status == "ready":
        if ik_status == "error":
            return _write_progress(
                session_id,
                stage="error",
                message="Biomechanical analysis did not complete",
                error="Analysis failed",
                ik_status=ik_status,
            )
        return _write_progress(
            session_id,
            stage="ready",
            message="Processing complete",
            ik_status=ik_status or "ready",
            opensim_motion=ptr.get("opensim_motion") or "",
        )
    if status == "running" and ik_status == "running":
        return _write_progress(
            session_id,
            stage="ik",
            percent=_stage_creep_percent("ik", prog),
            message="CeRebel Foundation is refining biomechanical insight…",
            ik_status="running",
        )
    if status == "running":
        return _write_progress(
            session_id,
            stage="gemx",
            percent=_stage_creep_percent("gemx", prog),
            message="CeRebel Foundation is building motion understanding…",
        )
    return prog


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("CEREBEL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "sessions").mkdir(parents=True, exist_ok=True)
    if not UI_DIR.is_dir():
        raise SystemExit(f"upload UI missing: {UI_DIR}")
    ensure_queue()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    LOG.info("upload server on %s:%s ui=%s data=%s", HOST, PORT, UI_DIR, DATA_DIR)
    server.serve_forever()


if __name__ == "__main__":
    main()
