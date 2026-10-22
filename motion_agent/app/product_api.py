"""Account, library, and upload routes for the consumer product."""

from __future__ import annotations

import cgi
import json
import logging
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from motion_agent.app.accounts import delete_account, login, logout, register, user_from_token
from motion_agent.app.config import Settings
from motion_agent.app.library import (
    add_capture,
    captures_today,
    delete_capture,
    list_captures,
    owner_of,
    owns,
    session_ids_for_user,
    update_status,
)

LOG = logging.getLogger("motion_agent.product_api")

_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".mpeg", ".mpg"}


def token_from_headers(headers: Any) -> str | None:
    auth = headers.get("Authorization") or headers.get("X-User-Token")
    if not auth:
        return None
    return str(auth).strip()


def current_user(settings: Settings, headers: Any) -> dict[str, Any] | None:
    return user_from_token(settings.product_dir, token_from_headers(headers))


def handle_auth_post(settings: Settings, path: str, body: dict[str, Any], headers: Any) -> tuple[int, dict[str, Any]]:
    if path == "/api/auth/register":
        try:
            return 200, register(settings.product_dir, str(body.get("email") or ""), str(body.get("password") or ""))
        except ValueError as exc:
            return 400, {"ok": False, "error": True, "message": str(exc)}
    if path == "/api/auth/login":
        try:
            return 200, login(settings.product_dir, str(body.get("email") or ""), str(body.get("password") or ""))
        except ValueError as exc:
            return 401, {"ok": False, "error": True, "message": str(exc)}
    if path == "/api/auth/logout":
        logout(settings.product_dir, token_from_headers(headers))
        return 200, {"ok": True}
    return 404, {"error": True, "message": "not found"}


def library_payload(settings: Settings, user: dict[str, Any]) -> dict[str, Any]:
    rows = list_captures(settings.product_dir, user["user_id"])
    for row in rows:
        row["progress"] = read_progress(settings, row.get("session_id") or "")
        stage = (row.get("progress") or {}).get("stage")
        if stage in {"ready", "error"} and row.get("status") != stage:
            update_status(settings.product_dir, row["session_id"], stage)
            row["status"] = stage
    return {
        "ok": True,
        "user": {"user_id": user["user_id"], "email": user["email"]},
        "captures": rows,
        "daily_limit": settings.invite_daily_limit,
        "used_today": captures_today(settings.product_dir, user["user_id"]),
    }


def user_may_view_session(settings: Settings, user: dict[str, Any] | None, session_id: str) -> bool:
    if user is None:
        return True
    return owns(settings.product_dir, user["user_id"], session_id)


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


def save_upload(
    settings: Settings,
    user: dict[str, Any],
    *,
    filename: str,
    file_obj: Any,
) -> tuple[int, dict[str, Any]]:
    if captures_today(settings.product_dir, user["user_id"]) >= settings.invite_daily_limit:
        return 429, {
            "ok": False,
            "error": True,
            "message": f"Daily capture limit is {settings.invite_daily_limit}. Try again tomorrow.",
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


def _run_pipeline(settings: Settings, session_id: str, filename: str) -> None:
    try:
        from upload_server import _process_uploaded_session

        _process_uploaded_session(session_id, filename)
        prog = read_progress(settings, session_id) or {}
        stage = str(prog.get("stage") or "processing")
        if stage == "ready":
            finalize_capture(settings, session_id)
        else:
            update_status(settings.product_dir, session_id, stage, message=prog.get("message"))
            if stage == "error":
                record_event(
                    settings,
                    "pipeline_error",
                    session_id=session_id,
                    message=str(prog.get("message") or prog.get("error") or ""),
                )
    except Exception as exc:  # noqa: BLE001
        LOG.exception("pipeline start failed")
        _write_progress(
            settings,
            session_id,
            stage="error",
            percent=100,
            message="Processing could not start on this machine.",
            error=str(exc),
        )
        update_status(settings.product_dir, session_id, "error", message=str(exc))
        record_event(settings, "pipeline_error", session_id=session_id, message=str(exc))


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
    filename = getattr(fileitem, "filename", None) or "video.mp4"
    return save_upload(settings, user, filename=filename, file_obj=fileitem.file)


def remove_account(settings: Settings, user: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    session_ids = session_ids_for_user(settings.product_dir, user["user_id"])
    delete_account(settings.product_dir, user["user_id"])
    for session_id in session_ids:
        delete_capture(settings.product_dir, user["user_id"], session_id)
        purge_session_dir(settings, session_id)
    record_event(settings, "account_deleted", user_id=user["user_id"], captures=len(session_ids))
    return 200, {"ok": True, "deleted_captures": len(session_ids)}


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


def media_allowed(settings: Settings, user: dict[str, Any] | None, session_id: str) -> bool:
    """Uploaded captures stay private. Sessions with no owner remain the shared demo."""
    owner = owner_of(settings.product_dir, session_id)
    if not owner:
        return True
    return bool(user) and user.get("user_id") == owner


def purge_session_dir(settings: Settings, session_id: str) -> None:
    root = (settings.data_dir / "sessions").resolve()
    target = (root / session_id).resolve()
    if root not in target.parents or not target.is_dir():
        return
    shutil.rmtree(target, ignore_errors=True)


def record_event(settings: Settings, kind: str, **fields: Any) -> None:
    settings.product_dir.mkdir(parents=True, exist_ok=True)
    path = settings.product_dir / "events.jsonl"
    row = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str) + "\n")
