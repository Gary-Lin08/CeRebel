"""Account, library, and upload routes for the consumer product."""

from __future__ import annotations

import cgi
import json
import logging
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from motion_agent.app.accounts import (
    delete_account,
    has_video_consent,
    login,
    logout,
    record_video_consent,
    register,
    touch_activity,
    user_from_token,
    video_consent_status,
)
from motion_agent.app.config import Settings
from motion_agent.app.file_lock import locked
from motion_agent.app.library import (
    add_capture,
    all_captures,
    captures_today,
    delete_capture,
    list_captures,
    log_practice as _log_practice,
    owner_of,
    owns,
    purge_user_rows,
    session_ids_for_user,
    update_status,
)

LOG = logging.getLogger("motion_agent.product_api")

_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".mpeg", ".mpg"}

MIN_DURATION_S = 1.5
MAX_DURATION_S = 30.0
MIN_FPS = 24.0
MIN_SHORT_SIDE = 320
LOW_FPS_WARN = 59.0
LOW_RES_WARN = 480


def probe_video(path: Path) -> dict[str, Any] | None:
    """Duration, frame rate and size via ffprobe; None when ffprobe is unavailable or fails."""
    import subprocess

    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate:format=duration",
        "-of", "json", str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    streams = data.get("streams") or []
    if not streams:
        return {"no_video": True}
    st = streams[0]

    def _rate(text: str | None) -> float:
        try:
            num, _, den = str(text or "0/1").partition("/")
            return float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            return 0.0

    fps = _rate(st.get("avg_frame_rate")) or _rate(st.get("r_frame_rate"))
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except ValueError:
        duration = 0.0
    return {
        "width": int(st.get("width") or 0),
        "height": int(st.get("height") or 0),
        "fps": round(fps, 2),
        "duration": round(duration, 2),
    }


def quality_check(meta: dict[str, Any] | None) -> tuple[str | None, list[str]]:
    """Return (rejection message or None, warnings). Unknown metadata passes."""
    if meta is None:
        return None, []
    if meta.get("no_video"):
        return "We couldn't find a video track in that file. Try exporting it again as MP4 or MOV.", []
    duration = float(meta.get("duration") or 0)
    fps = float(meta.get("fps") or 0)
    short_side = min(int(meta.get("width") or 0), int(meta.get("height") or 0))
    if duration and duration < MIN_DURATION_S:
        return f"That clip is only {duration:.1f}s. Film the whole swing, from address to finish.", []
    if duration > MAX_DURATION_S:
        return (
            f"That clip is {duration:.0f}s long. Trim it to one swing (under {MAX_DURATION_S:.0f}s) and upload again.",
            [],
        )
    if fps and fps < MIN_FPS:
        return f"That video is {fps:.0f} fps, which is too choppy to measure. Use your camera's normal or slow-motion mode.", []
    if short_side and short_side < MIN_SHORT_SIDE:
        return "That video is too small to measure. Upload the original from your camera roll, not a compressed copy.", []
    warnings = []
    if fps and fps < LOW_FPS_WARN:
        warnings.append("Filmed below 60 fps: fast moments like impact are less precise. Slow-motion works best.")
    if short_side and short_side < LOW_RES_WARN:
        warnings.append("Low resolution: some joint angles may be less precise.")
    return None, warnings


def token_from_headers(headers: Any) -> str | None:
    auth = headers.get("Authorization") or headers.get("X-User-Token")
    if not auth:
        return None
    return str(auth).strip()


def current_user(settings: Settings, headers: Any) -> dict[str, Any] | None:
    user = user_from_token(settings.product_dir, token_from_headers(headers))
    if user and user.get("user_id"):
        # Any signed-in request counts as an interaction for the inactivity-deletion clock.
        try:
            touch_activity(settings.product_dir, str(user["user_id"]))
        except OSError:
            LOG.warning("could not record account activity")
    return user


def handle_video_consent(settings: Settings, user: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST /api/account/video-consent {"accept": true, "version": "..."}."""
    if body.get("accept") is not True:
        return 400, {
            "ok": False,
            "error": True,
            "message": "Tick the box to agree to video processing before uploading.",
            "video_consent": video_consent_status(settings.product_dir, user["user_id"]),
        }
    try:
        accepted = record_video_consent(settings.product_dir, user["user_id"], str(body.get("version") or ""))
    except ValueError as exc:
        return 409, {
            "ok": False,
            "error": True,
            "message": str(exc),
            "video_consent": video_consent_status(settings.product_dir, user["user_id"]),
        }
    except LookupError as exc:
        return 404, {"ok": False, "error": True, "message": str(exc)}
    record_event(settings, "video_consent", user_id=user["user_id"], version=accepted["version"])
    return 200, {"ok": True, "video_consent": video_consent_status(settings.product_dir, user["user_id"])}


def handle_auth_post(settings: Settings, path: str, body: dict[str, Any], headers: Any,
                     client_ip: str = "unknown") -> tuple[int, dict[str, Any]]:
    from motion_agent.app import rate_limit

    if path == "/api/auth/register":
        if rate_limit.SIGNUPS_PER_IP.blocked(client_ip):
            return 429, {"ok": False, "error": True, "message": rate_limit.TOO_MANY_SIGNUPS}
        try:
            created = register(
                settings.product_dir,
                str(body.get("email") or ""),
                str(body.get("password") or ""),
                accepted_terms=body.get("accept_terms") is True,
                coach_code=body.get("coach_code"),
            )
        except ValueError as exc:
            return 400, {"ok": False, "error": True, "message": str(exc)}
        rate_limit.SIGNUPS_PER_IP.hit(client_ip)
        record_event(
            settings,
            "signup",
            user_id=created["user"]["user_id"],
            coach_code=bool(body.get("coach_code")),
        )
        return 200, created
    if path == "/api/auth/login":
        email_key = str(body.get("email") or "").strip().lower()[:254]
        if rate_limit.LOGIN_FAILS_PER_EMAIL.blocked(email_key) or rate_limit.LOGIN_FAILS_PER_IP.blocked(client_ip):
            return 429, {"ok": False, "error": True, "message": rate_limit.TOO_MANY_LOGINS}
        try:
            result = login(settings.product_dir, email_key, str(body.get("password") or ""))
        except ValueError as exc:
            rate_limit.LOGIN_FAILS_PER_EMAIL.hit(email_key)
            rate_limit.LOGIN_FAILS_PER_IP.hit(client_ip)
            return 401, {"ok": False, "error": True, "message": str(exc)}
        rate_limit.LOGIN_FAILS_PER_EMAIL.clear(email_key)
        return 200, result
    if path == "/api/auth/logout":
        logout(settings.product_dir, token_from_headers(headers))
        return 200, {"ok": True}
    return 404, {"error": True, "message": "not found"}


def library_payload(settings: Settings, user: dict[str, Any]) -> dict[str, Any]:
    rows = list_captures(settings.product_dir, user["user_id"])
    for row in rows:
        row["progress"] = read_progress_with_queue(settings, row.get("session_id") or "")
        stage = (row.get("progress") or {}).get("stage")
        if stage in {"ready", "error"} and row.get("status") != stage:
            update_status(settings.product_dir, row["session_id"], stage)
            row["status"] = stage
    from motion_agent.app import billing
    from motion_agent.app.compare import library_compare_block

    return {
        "ok": True,
        "user": {"user_id": user["user_id"], "email": user["email"]},
        "captures": rows,
        "compare": library_compare_block(settings, user["user_id"], rows),
        "quota": billing.quota(settings.product_dir, user["user_id"], rows),
        "subscription": _public_subscription(settings, user["user_id"]),
        "billing": billing.public_plans(settings.product_dir),
        "video_consent": video_consent_status(settings.product_dir, user["user_id"]),
    }


def _public_subscription(settings: Settings, user_id: str) -> dict[str, Any]:
    from motion_agent.app import billing

    row = billing.subscription_for(settings.product_dir, user_id)
    return {
        "pro": billing.is_pro(settings.product_dir, user_id),
        "status": row.get("status"),
        "plan": row.get("plan"),
        "current_period_end": row.get("current_period_end"),
        "cancel_at_period_end": bool(row.get("cancel_at_period_end")),
        "can_manage": bool(row.get("customer")),
    }


def user_may_view_session(settings: Settings, user: dict[str, Any] | None, session_id: str) -> bool:
    """Only an explicitly configured demo or the account's own capture is visible."""
    if session_id == configured_demo_session_id(settings):
        return True
    return bool(user) and owns(settings.product_dir, user["user_id"], session_id)


def configured_demo_session_id(settings: Settings) -> str | None:
    """Never infer a public demo from unowned legacy sessions."""
    sid = os.environ.get("CEREBEL_DEMO_SESSION_ID", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        return None
    motion = settings.data_dir / "sessions" / sid / "ik" / "opensim-motion.json"
    return sid if motion.is_file() and owner_of(settings.product_dir, sid) is None else None


def default_visible_session_id(
    settings: Settings, data_dir: Path, user: dict[str, Any] | None
) -> str | None:
    """Return the account's newest ready capture, then the configured demo."""
    sessions_root = Path(data_dir) / "sessions"

    def _ready(sid: str) -> bool:
        return (sessions_root / sid / "ik" / "opensim-motion.json").is_file()

    if user:
        for row in list_captures(settings.product_dir, user["user_id"]):
            sid = str(row.get("session_id") or "")
            if sid and _ready(sid):
                return sid
    return configured_demo_session_id(settings)


AVG_JOB_SECONDS = float(os.environ.get("CEREBEL_AVG_JOB_SECONDS", "150"))


def queue_info(session_id: str) -> dict[str, Any] | None:
    """Place in the GPU line and a rough wait, when this process owns the queue."""
    import sys

    mod = sys.modules.get("upload_server")
    queue = getattr(mod, "_QUEUE", None) if mod else None
    if queue is None or not hasattr(queue, "jobs_ahead"):
        return None
    found = queue.jobs_ahead(session_id)
    if found is None:
        return None
    ahead, running = found
    workers = max(1, int(getattr(queue, "workers", 1) or 1))
    wait_s = 0.5 * AVG_JOB_SECONDS if running else (ahead // workers + 1.5) * AVG_JOB_SECONDS
    minutes = max(1, round(wait_s / 60))
    if running:
        message = f"Rebuilding your body now — about {minutes} min."
    elif ahead == 0:
        message = f"You're next — about {minutes} min."
    else:
        from motion_agent.app.notify import enabled as email_enabled

        message = f"{ahead} swing{'s' if ahead != 1 else ''} ahead of you — about {minutes} min."
        # Only promise an email when the server can actually send one.
        if email_enabled():
            message += " We'll email you when it's ready."
    return {"jobs_ahead": ahead, "running": running, "eta_minutes": minutes, "queue_message": message}


PIPELINE_FAILED_MESSAGE = "Something went wrong on our side while analyzing this video. Please try again."
GPU_DOWN_MESSAGE = (
    "Swing analysis is offline for a few minutes. Nothing was uploaded and it didn't use one of "
    "your analyses. Please try again shortly."
)


def gpu_status() -> str:
    """State of the GPU that analyses uploads: "ready", "loading", "remote" (Modal), "down",
    or "unknown" when the pipeline code is not importable here."""
    try:
        from gemx_pipeline import worker_status
    except ImportError:
        return "unknown"
    return worker_status()


def public_progress(prog: dict[str, Any] | None) -> dict[str, Any] | None:
    """Progress as shown to the user: a raw exception text (message == error) is replaced."""
    if not prog or prog.get("stage") != "error":
        return prog
    out = {k: v for k, v in prog.items() if k != "error"}
    raw = prog.get("error")
    if not out.get("message") or (raw and out.get("message") == raw):
        out["message"] = PIPELINE_FAILED_MESSAGE
    return out


def read_progress_with_queue(settings: Settings, session_id: str) -> dict[str, Any] | None:
    prog = public_progress(read_progress(settings, session_id))
    if prog and str(prog.get("stage") or "") not in {"ready", "error", "no_swing", "rejected"}:
        info = queue_info(session_id)
        if info:
            prog = {**prog, **info}
    return prog


def read_progress(settings: Settings, session_id: str) -> dict[str, Any] | None:
    path = settings.data_dir / "sessions" / session_id / "upload_progress.json"
    if not path.is_file():
        ready = settings.data_dir / "sessions" / session_id / "ik" / "opensim-motion.json"
        if ready.is_file():
            return {"session_id": session_id, "stage": "ready", "percent": 100, "message": "Ready to coach"}
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _parse_trim(trim: tuple[Any, Any] | None, duration: float) -> tuple[float, float] | None:
    """Validate a client-proposed swing window; None means keep the whole clip."""
    if not trim:
        return None
    try:
        start, end = float(trim[0]), float(trim[1])
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    start = max(0.0, start)
    end = min(duration, end)
    length = end - start
    if length < MIN_DURATION_S or length > 10.0 or length >= duration - 0.25:
        return None
    return round(start, 3), round(end, 3)


def trim_video(src: Path, start: float, end: float) -> Path | None:
    """Cut the swing window with ffmpeg (re-encode for frame accuracy). None on failure."""
    import subprocess

    dest = src.with_name("swing_window.mp4")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
        "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 1024:
        dest.unlink(missing_ok=True)
        return None
    return dest


def save_upload(
    settings: Settings,
    user: dict[str, Any],
    *,
    filename: str,
    file_obj: Any,
    trim: tuple[Any, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    from motion_agent.app import billing

    rows = list_captures(settings.product_dir, user["user_id"])
    q = billing.quota(settings.product_dir, user["user_id"], rows)
    if q["remaining"] <= 0:
        record_event(settings, "paywall_hit", user_id=user["user_id"], plan=q["plan"])
        if q["plan"] == "pro":
            message = (
                f"You've reached this month's fair-use limit of {q['monthly_limit']} analyses. "
                "It resets on the 1st."
            )
        else:
            message = (
                f"You've used your {q['monthly_limit']} free analyses this month. "
                "Upgrade to Pro for more, or wait until the 1st."
            )
        return 402, {"ok": False, "error": True, "paywall": q["plan"] == "free", "quota": q, "message": message}
    if not has_video_consent(settings.product_dir, user["user_id"]):
        # Nothing is written to disk before the account has agreed to the current video text.
        record_event(settings, "upload_needs_consent", user_id=user["user_id"])
        return 428, {
            "ok": False,
            "error": True,
            "consent_required": True,
            "video_consent": video_consent_status(settings.product_dir, user["user_id"]),
            "message": "Please review and agree to how your video is processed before your first upload.",
        }
    ext = Path(filename or "video.mp4").suffix.lower() or ".mp4"
    if ext not in _VIDEO_EXT:
        return 400, {"ok": False, "error": True, "message": f"Unsupported video type: {ext}"}
    session_id = str(uuid.uuid4()).upper()
    session_dir = settings.data_dir / "sessions" / session_id
    upload_dir = session_dir / "upload"
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / f"source_video{ext}"
    _write_progress(settings, session_id, stage="uploading", percent=8, message="Receiving upload…", filename=filename)
    try:
        with dest.open("wb") as out:
            shutil.copyfileobj(file_obj, out, length=1024 * 1024)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("upload save failed")
        _write_progress(settings, session_id, stage="error", percent=100, message=str(exc), error=str(exc))
        return 500, {"ok": False, "error": True, "message": str(exc), "session_id": session_id}
    size = dest.stat().st_size
    if size < 1024:
        _write_progress(settings, session_id, stage="error", percent=100, message="File is too small", error="too small")
        return 400, {"ok": False, "error": True, "message": "File is too small", "session_id": session_id}
    if size > settings.max_upload_bytes:
        dest.unlink(missing_ok=True)
        return 413, {"ok": False, "error": True, "message": "File is too large"}
    meta = probe_video(dest)
    rejection, warnings = quality_check(meta)
    if rejection:
        # Rejected uploads are recorded (for the funnel) but never count toward the quota,
        # and the video itself is removed right away.
        add_capture(
            settings.product_dir,
            user_id=user["user_id"],
            session_id=session_id,
            filename=filename or dest.name,
            status="rejected",
        )
        update_status(settings.product_dir, session_id, "rejected", message=rejection)
        purge_session_dir(settings, session_id)
        record_event(settings, "upload_rejected", user_id=user["user_id"], reason=rejection[:80])
        return 422, {"ok": False, "error": True, "rejected": True, "message": rejection + " This didn't use one of your analyses."}
    window = _parse_trim(trim, float((meta or {}).get("duration") or 0.0))
    trimmed = None
    if window:
        trimmed = trim_video(dest, *window)
        if trimmed is not None:
            # Only the swing window is kept and sent to the GPU: less compute, less stored video.
            dest.unlink(missing_ok=True)
            dest = trimmed.replace(upload_dir / "source_video.mp4")
            record_event(settings, "upload_trimmed", user_id=user["user_id"], seconds=round(window[1] - window[0], 2))
    add_capture(
        settings.product_dir,
        user_id=user["user_id"],
        session_id=session_id,
        filename=filename or dest.name,
        status="processing",
    )
    _write_progress(
        settings,
        session_id,
        stage="extracting",
        percent=12,
        message="Upload saved — preparing your swing…",
        filename=filename,
        bytes=size,
    )
    record_event(settings, "upload_saved", user_id=user["user_id"], session_id=session_id, bytes=size)
    threading.Thread(
        target=_run_pipeline,
        args=(settings, session_id, filename or dest.name),
        name=f"product-upload-{session_id[:8]}",
        daemon=True,
    ).start()
    return 200, {
        "ok": True,
        "session_id": session_id,
        "filename": filename,
        "bytes": size,
        "stage": "extracting",
        "warnings": warnings,
    }


def _write_progress(settings: Settings, session_id: str, **fields: Any) -> None:
    path = settings.data_dir / "sessions" / session_id / "upload_progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"session_id": session_id, **fields}
    path.write_text(json.dumps(payload), encoding="utf-8")


def finalize_capture(settings: Settings, session_id: str) -> dict[str, Any]:
    """After IK is ready, run Sport Pack detect and stamp the library row."""
    session_dir = settings.data_dir / "sessions" / session_id
    motion = session_dir / "ik" / "opensim-motion.json"
    if not motion.is_file():
        update_status(settings.product_dir, session_id, "error", message="Body reconstruction unfinished")
        return {"status": "error", "message": "Body reconstruction unfinished"}

    from motion_agent.sports.registry import resolve_pack_session

    pack = resolve_pack_session(session_dir, video_id=session_id, data_dir=settings.data_dir)
    fields = pack.as_session_fields()
    sport = fields.get("sport") or "general"
    conf = float(fields.get("sport_confidence") or 0.0)
    message = (fields.get("detect_message") or "").strip()
    consumer_ready = bool(fields.get("consumer_ready") and sport != "general")
    if sport == "general":
        status = "no_swing"
        if not message:
            message = (
                "We rebuilt the body but couldn’t confirm a golf swing. "
                "Try a side-on clip that shows a full swing."
            )
        consumer_ready = False
    else:
        status = "ready"
        message = message or f"Detected {fields.get('sport_label') or sport}."

    update_status(
        settings.product_dir,
        session_id,
        status,
        sport=sport,
        sport_label=fields.get("sport_label"),
        sport_confidence=conf,
        detect_message=message,
        consumer_ready=consumer_ready,
    )
    _write_progress(
        settings,
        session_id,
        stage=status,
        percent=100,
        message=message,
        sport=sport,
        sport_confidence=conf,
        consumer_ready=consumer_ready,
    )
    record_event(
        settings,
        "capture_finalized",
        session_id=session_id,
        sport=sport,
        sport_confidence=conf,
        status=status,
    )
    return {
        "status": status,
        "sport": sport,
        "sport_confidence": conf,
        "message": message,
        "consumer_ready": consumer_ready,
    }


def _email_owner(settings: Settings, session_id: str, status: str, detail: str) -> None:
    try:
        from motion_agent.app.accounts import profile
        from motion_agent.app.notify import notify_analysis

        owner = owner_of(settings.product_dir, session_id)
        email = (profile(settings.product_dir, owner) or {}).get("email") if owner else None
        if notify_analysis(email, status, session_id, detail):
            record_event(settings, "email_sent", session_id=session_id, status=status)
    except Exception:  # noqa: BLE001
        LOG.exception("analysis email failed session=%s", session_id)


PIPELINE_TIMEOUT_S = 45 * 60
_TERMINAL_STAGES = {"ready", "error", "no_swing"}


def _wait_for_pipeline(settings: Settings, session_id: str, *, poll_s: float = 5.0) -> dict[str, Any]:
    """Block (in the upload thread) until the GPU pipeline reaches a final stage."""
    import time

    deadline = time.monotonic() + PIPELINE_TIMEOUT_S
    while True:
        prog = read_progress(settings, session_id) or {}
        if str(prog.get("stage") or "") in _TERMINAL_STAGES:
            return prog
        if time.monotonic() >= deadline:
            _write_progress(
                settings,
                session_id,
                stage="error",
                percent=100,
                message="Processing took too long. This upload does not count toward your monthly analyses.",
                error="timeout",
                failed_at="timeout",
            )
            return read_progress(settings, session_id) or {"stage": "error"}
        time.sleep(poll_s)


def _run_pipeline(settings: Settings, session_id: str, filename: str) -> None:
    try:
        from upload_server import _process_uploaded_session

        _process_uploaded_session(session_id, filename)
        prog = _wait_for_pipeline(settings, session_id)
        if capture_was_deleted(settings, session_id):
            # Deleted while the GPU was working: the pipeline wrote results back after the purge.
            purge_session_dir(settings, session_id)
            return
        stage = str(prog.get("stage") or "processing")
        if stage == "ready":
            final = finalize_capture(settings, session_id) or {}
            _email_owner(settings, session_id, "ready" if final.get("status") == "ready" else "failed",
                         str(final.get("message") or ""))
        else:
            update_status(settings.product_dir, session_id, stage, message=(public_progress(prog) or {}).get("message"))
            if stage == "error":
                _email_owner(settings, session_id, "failed", "")
                record_event(
                    settings,
                    "pipeline_error",
                    session_id=session_id,
                    failed_at=str(prog.get("failed_at") or "unknown"),
                    message=str(prog.get("error") or prog.get("message") or ""),
                )
    except Exception as exc:  # noqa: BLE001
        LOG.exception("pipeline start failed")
        if capture_was_deleted(settings, session_id):
            purge_session_dir(settings, session_id)
            return
        _write_progress(
            settings,
            session_id,
            stage="error",
            percent=100,
            message="Processing could not start on this machine.",
            error=str(exc),
            failed_at="start",
        )
        update_status(settings.product_dir, session_id, "error", message=PIPELINE_FAILED_MESSAGE)
        record_event(settings, "pipeline_error", session_id=session_id, failed_at="start", message=str(exc))


def parse_upload(handler: Any, settings: Settings, user: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return 400, {"ok": False, "error": True, "message": "Empty upload"}
    if length > settings.max_upload_bytes:
        return 413, {"ok": False, "error": True, "message": "File is too large"}
    ctype = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in ctype:
        return 400, {"ok": False, "error": True, "message": "Expected a video file upload"}
    form = cgi.FieldStorage(
        fp=handler.rfile,
        headers=handler.headers,
        environ={
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": ctype,
            "CONTENT_LENGTH": str(length),
        },
        keep_blank_values=True,
    )
    fileitem = form["file"] if "file" in form else None
    if fileitem is None or not getattr(fileitem, "file", None):
        return 400, {"ok": False, "error": True, "message": "Missing file"}
    # Checked after the body is read (an early reply can surface as a network error in the
    # browser) and before anything is saved: without a GPU the job could only wait and fail.
    if gpu_status() == "down":
        record_event(settings, "upload_unavailable", user_id=user["user_id"], reason="gpu_down")
        return 503, {"ok": False, "error": True, "unavailable": True, "message": GPU_DOWN_MESSAGE}
    filename = getattr(fileitem, "filename", None) or "video.mp4"
    trim = None
    if "trim_start" in form and "trim_end" in form:
        trim = (form.getfirst("trim_start"), form.getfirst("trim_end"))
    return save_upload(settings, user, filename=filename, file_obj=fileitem.file, trim=trim)


def remove_account(settings: Settings, user: dict[str, Any], *, reason: str = "user_request") -> tuple[int, dict[str, Any]]:
    """Delete the account, its tokens, every capture (files and GPU results), its library rows,
    its local billing row and its usage events. Stripe keeps its own payment records."""
    from motion_agent.app import billing

    user_id = user["user_id"]
    # Includes past_due / unpaid: Stripe keeps retrying the card, and once the account is gone
    # the user has no Manage subscription button left to stop it.
    if billing.still_billing(settings.product_dir, user_id):
        return 409, {
            "ok": False,
            "error": True,
            "subscription_active": True,
            "message": "Cancel your Pro subscription first (Manage subscription) so you are not charged again, then delete your account.",
        }
    active = session_ids_for_user(settings.product_dir, user_id)
    # Include rows deleted earlier: their GPU results may still be on disk.
    every = sorted({str(r["session_id"]) for r in all_captures(settings.product_dir)
                    if r.get("user_id") == user_id and r.get("session_id")})
    delete_account(settings.product_dir, user_id)
    for session_id in every:
        purge_session_dir(settings, session_id)
    purge_user_rows(settings.product_dir, user_id)
    _forget_billing(settings.product_dir, user_id)
    scrub_events(settings, user_id=user_id, session_ids=every)
    # No user id here: the point is that nothing links back to the deleted account.
    record_event(settings, "account_deleted", captures=len(active), reason=reason)
    return 200, {"ok": True, "deleted_captures": len(active)}


def _forget_billing(product_dir: Path, user_id: str) -> None:
    from motion_agent.app import billing

    billing.forget_user(product_dir, user_id)


# Thread lock for this process; file_lock.locked adds the flock that retention and
# privacy_backfill (separate processes rewriting events.jsonl) also take.
_EVENTS_LOCK = threading.Lock()


def scrub_events(settings: Settings, *, user_id: str | None = None, session_ids: list[str] | None = None,
                 older_than: datetime | None = None) -> int:
    """Drop usage-log rows for a deleted account / session, or rows older than a cutoff."""
    path = settings.product_dir / "events.jsonl"
    if not path.is_file():
        return 0
    sids = set(session_ids or [])
    kept: list[str] = []
    removed = 0
    with locked(path, _EVENTS_LOCK):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            drop = bool(user_id) and row.get("user_id") == user_id
            drop = drop or (bool(sids) and row.get("session_id") in sids)
            if older_than is not None and not drop:
                try:
                    ts = datetime.fromisoformat(str(row.get("ts")))
                    drop = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)) < older_than
                except (TypeError, ValueError):
                    pass
            if drop:
                removed += 1
            else:
                kept.append(line)
        if removed:
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(k + "\n" for k in kept), encoding="utf-8")
            tmp.replace(path)
    return removed


def ops_summary(settings: Settings) -> dict[str, Any]:
    path = settings.product_dir / "events.jsonl"
    counts: dict[str, int] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                kind = str(json.loads(line).get("kind") or "unknown")
            except json.JSONDecodeError:
                kind = "bad_row"
            counts[kind] = counts.get(kind, 0) + 1
    uploads = counts.get("upload_saved", 0)
    failures = counts.get("pipeline_error", 0)
    return {
        "ok": True,
        "counts": counts,
        "upload_failure_rate": round(failures / uploads, 3) if uploads else 0.0,
    }


def remove_capture(settings: Settings, user: dict[str, Any], session_id: str) -> tuple[int, dict[str, Any]]:
    if not delete_capture(settings.product_dir, user["user_id"], session_id):
        return 404, {"ok": False, "error": True, "message": "Capture not found"}
    purge_session_dir(settings, session_id)
    record_event(settings, "capture_deleted", user_id=user["user_id"], session_id=session_id)
    return 200, {"ok": True, "session_id": session_id}


_FINDING_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def log_practice(settings: Settings, user: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """The user marked a fix's drill as practiced on one of their captures."""
    session_id = str(body.get("session_id") or "")
    finding_id = str(body.get("finding_id") or "")
    if not _FINDING_ID.match(finding_id):
        return 400, {"ok": False, "error": True, "message": "Unknown fix"}
    count = _log_practice(settings.product_dir, user["user_id"], session_id, finding_id)
    if count is None:
        return 404, {"ok": False, "error": True, "message": "Capture not found"}
    record_event(settings, "practice_logged", user_id=user["user_id"], session_id=session_id, finding_id=finding_id)
    return 200, {"ok": True, "session_id": session_id, "finding_id": finding_id, "count": count}


def media_session_live(settings: Settings, session_id: str) -> bool:
    """The demo, or a capture whose library row still exists and is not deleted."""
    if session_id == configured_demo_session_id(settings):
        return True
    owner = owner_of(settings.product_dir, session_id)
    return bool(owner) and owns(settings.product_dir, owner, session_id)


def capture_was_deleted(settings: Settings, session_id: str) -> bool:
    """True when this id was a real capture that its owner (or retention) deleted."""
    return bool(owner_of(settings.product_dir, session_id)) and not media_session_live(settings, session_id)


def media_allowed(settings: Settings, user: dict[str, Any] | None, session_id: str) -> bool:
    """Only the chosen demo and captures owned by this account are public/readable."""
    return user_may_view_session(settings, user, session_id)


def gemx_output_root() -> Path:
    gemx_root = Path(os.environ.get("CEREBEL_GEMX_ROOT", "/root/GEM-X"))
    return Path(os.environ.get("CEREBEL_GEMX_OUTPUT_ROOT", str(gemx_root / "outputs" / "cerebel")))


def neck_output_root() -> Path:
    """Where older builds of the neck IK step left soma_raw/soma_aligned.npz per session.
    New captures write these into session/ik/ instead; this folder is only cleaned up."""
    default = Path(__file__).resolve().parents[2] / "kinetic-lens" / "retargeting_neck_v2" / "outputs"
    return Path(os.environ.get("CEREBEL_NECK_OUTPUT_ROOT", str(default)))


def gemx_output_dir(session_id: str) -> Path | None:
    """GEM-X writes its results outside the session folder (CEREBEL_GEMX_OUTPUT_ROOT/<session>)."""
    return _session_subdir(gemx_output_root(), session_id)


def neck_output_dir(session_id: str) -> Path | None:
    return _session_subdir(neck_output_root(), session_id)


def _session_subdir(root: Path, session_id: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id or ""):
        return None
    try:
        root = root.resolve()
        target = (root / session_id).resolve()
        if root in target.parents and target.is_dir():
            return target
    except OSError:
        return None
    return None


def purge_session_dir(settings: Settings, session_id: str) -> None:
    for outside in (gemx_output_dir(session_id), neck_output_dir(session_id)):
        if outside is not None:
            shutil.rmtree(outside, ignore_errors=True)
    root = (settings.data_dir / "sessions").resolve()
    target = (root / session_id).resolve()
    if root not in target.parents or not target.is_dir():
        return
    shutil.rmtree(target, ignore_errors=True)


def record_event(settings: Settings, kind: str, **fields: Any) -> None:
    settings.product_dir.mkdir(parents=True, exist_ok=True)
    path = settings.product_dir / "events.jsonl"
    row = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
    # Open only once the lock is held, so a concurrent rewrite cannot leave us appending to the old file.
    with locked(path, _EVENTS_LOCK), path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str) + "\n")


# Events the browser may send. Anything else is dropped.
CLIENT_EVENTS = {
    "landing_view",
    "sample_report_view",
    "signup_view",
    "upload_click",
    "report_view",
    "fix_expand",
    "trust_yes",
    "trust_no",
    "chat_open",
    "paywall_view",
    "checkout_click",
    "share_click",
    "progress_card_view",
    "progress_open",
}


def record_client_event(settings: Settings, user: dict[str, Any] | None, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    name = str(body.get("event") or "")[:40]
    if name not in CLIENT_EVENTS:
        return 400, {"ok": False, "error": True, "message": "unknown event"}
    props = body.get("props") if isinstance(body.get("props"), dict) else {}
    reserved = {"kind", "ts", "user_id", "visitor", "settings"}
    safe = {
        str(k)[:40]: v
        for k, v in list(props.items())[:10]
        if str(k)[:40] not in reserved
        and isinstance(v, (str, int, float, bool))
        and (not isinstance(v, str) or len(v) <= 120)
    }
    record_event(
        settings,
        "client_" + name,
        user_id=(user or {}).get("user_id"),
        visitor=str(body.get("visitor") or "")[:40] or None,
        **safe,
    )
    return 200, {"ok": True}
