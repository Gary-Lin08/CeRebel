"""Minimal FastAPI-free HTTP chat server for the motion reasoning demo."""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from motion_agent.app.config import load_settings
from motion_agent.app.health import health_report
from motion_agent.app.routes import EXAMPLE_QUESTIONS, chat_payload, sse_pack, trace_to_dict
from motion_agent.llm.agent import (
    answer_question,
    get_current_session_info,
    get_golf_phase_cards,
    get_sport_analysis,
)
from motion_agent.llm.prompts import (
    DEFAULT_PROMPT_MODE,
    ORIGINAL_SYSTEM_PROMPT,
    list_prompt_presets,
)
from motion_agent.llm.qwen_client import build_vlm
from motion_agent.mesh.opensim_clip import get_smplx_mesh_clip, smplx_mesh_status
from motion_agent.storage.motion_store import newest_ready_session_dir
from motion_agent.storage.video_store import resolve_media_file

LOG = logging.getLogger("motion_agent.chat_server")

UI_DIR = Path(__file__).resolve().parent / "ui"
SETTINGS = load_settings()
DATA_DIR = SETTINGS.data_dir
HOST = SETTINGS.host
PORT = SETTINGS.port
BACKEND = SETTINGS.backend
# Browser origins allowed to call this API cross-site (comma-separated). Empty = same-origin only.
ALLOWED_ORIGINS = frozenset(
    origin.strip().rstrip("/")
    for origin in os.environ.get("CEREBEL_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
)


def _public_presets() -> list[dict]:
    """Preset metadata without prompt bodies (password-gated)."""
    return [
        {
            "id": p["id"],
            "label": p["label"],
            "description": p["description"],
        }
        for p in list_prompt_presets()
    ]


def _capture_visible(handler: BaseHTTPRequestHandler, session_id: str) -> bool:
    from motion_agent.app.product_api import current_user, media_allowed

    return media_allowed(SETTINGS, current_user(SETTINGS, handler.headers), session_id)


def _scoped_video_id(
    handler: BaseHTTPRequestHandler, requested: str | None
) -> tuple[str | None, tuple[int, dict] | None]:
    """Choose the capture a read endpoint may use.

    An explicit id must be visible to the caller. Without one, use the caller's newest
    capture (signed in) or the shared demo, never the globally newest session, which
    may belong to another account. Returns (video_id, error) with error = (code, payload).
    """
    from motion_agent.app.product_api import current_user, default_visible_session_id

    if requested:
        if not _capture_visible(handler, requested):
            return None, (403, {"error": True, "message": "This capture is private"})
        return requested, None
    sid = default_visible_session_id(SETTINGS, DATA_DIR, current_user(SETTINGS, handler.headers))
    if not sid:
        return None, (
            404,
            {
                "error": True,
                "type": "NO_SESSION",
                "fallback": "raw_video",
                "message": "No preprocessed video session found.",
            },
        )
    return sid, None


def _check_prompt_password(provided: str | None) -> bool:
    """Operator features stay locked until MOTION_AGENT_PROMPT_PASSWORD is set."""
    expected = SETTINGS.prompt_password
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _password_from_request(handler: BaseHTTPRequestHandler, parsed=None) -> str | None:
    """Read the operator password from the X-Prompt-Password header only.

    Query-string passwords end up in access logs and browser history, so they are ignored.
    """
    header = handler.headers.get("X-Prompt-Password")
    if header:
        return header.strip()
    return None


def _send_cors_headers(handler: BaseHTTPRequestHandler) -> None:
    """Echo the Origin only when it is explicitly allowed; never send a wildcard."""
    origin = (handler.headers.get("Origin") or "").rstrip("/")
    if origin and origin in ALLOWED_ORIGINS:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")


def _internal_error(exc: BaseException, where: str) -> dict:
    """Log the full traceback server-side; give the client only an opaque reference."""
    error_id = uuid.uuid4().hex[:12]
    LOG.error("%s failed [%s]: %s", where, error_id, exc, exc_info=exc)
    return {"error": True, "message": "Something went wrong. Please try again.", "error_id": error_id}


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


LEGAL_DIR = UI_DIR / "legal"


def _legal_html(name: str) -> bytes | None:
    """Terms/Privacy pages with operator details filled in from the environment."""
    path = LEGAL_DIR / f"{name}.html"
    if not path.is_file():
        return None
    html = path.read_text(encoding="utf-8")
    fills = {
        "{{COMPANY}}": os.environ.get("CEREBEL_LEGAL_ENTITY") or "CeRebel, Inc.",
        "{{CONTACT_EMAIL}}": os.environ.get("CEREBEL_SUPPORT_EMAIL") or "support@cerebel.ai",
    }
    from motion_agent.app.consent import inactive_delete_days, period_label, raw_video_retention_days

    fills["{{RAW_VIDEO_RETENTION_DAYS}}"] = str(raw_video_retention_days())
    fills["{{INACTIVE_DELETE_PERIOD}}"] = period_label(inactive_delete_days())
    for key, value in fills.items():
        html = html.replace(key, value)
    return html.encode("utf-8")


def _sign_media(handler: BaseHTTPRequestHandler, payload: dict) -> dict:
    """Sign /media/<session>/ URLs the caller may view, so <video> and loaders can fetch them."""
    from motion_agent.app.media_urls import sign_payload
    from motion_agent.app.product_api import current_user, media_allowed

    user = current_user(SETTINGS, handler.headers)
    return sign_payload(SETTINGS.product_dir, payload, lambda sid: media_allowed(SETTINGS, user, sid))


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    raw = json.dumps(payload, default=str).encode("utf-8")
    if code < 400 and b'"/media/' in raw:
        raw = json.dumps(_sign_media(handler, payload), default=str).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    _send_cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        LOG.info("%s - " + fmt, self.address_string(), *args)

    def _serve_legal(self, name: str) -> None:
        raw = _legal_html(name)
        if raw is None:
            return _json_response(self, 404, {"error": True, "message": "not found"})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        _send_cors_headers(self)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Prompt-Password, X-Motion-Dev, Authorization, X-User-Token",
        )
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            return self._serve_file(UI_DIR / "index.html")
        if path in ("/terms", "/privacy"):
            return self._serve_legal(path.strip("/"))
        if path == "/api/billing":
            from motion_agent.app.product_api import current_user, library_payload

            user = current_user(SETTINGS, self.headers)
            if not user:
                from motion_agent.app.billing import public_plans

                return _json_response(self, 200, {"ok": True, "user": None, "billing": public_plans(SETTINGS.product_dir)})
            full = library_payload(SETTINGS, user)
            return _json_response(
                self,
                200,
                {k: full[k] for k in ("ok", "user", "quota", "subscription", "billing")},
            )
        if path.startswith("/static/"):
            return self._serve_file(UI_DIR / path[len("/static/") :])
        if path == "/api/health":
            code, payload = health_report(SETTINGS)
            return _json_response(self, code, payload)
        if path == "/api/ops":
            password = _password_from_request(self, parsed)
            if not _check_prompt_password(password):
                return _json_response(self, 401, {"error": True, "message": "Password required"})
            from motion_agent.app.product_api import ops_summary

            return _json_response(self, 200, ops_summary(SETTINGS))
        if path == "/api/me":
            from motion_agent.app.product_api import current_user, library_payload

            user = current_user(SETTINGS, self.headers)
            if not user:
                return _json_response(self, 200, {"ok": True, "user": None, "captures": []})
            return _json_response(self, 200, library_payload(SETTINGS, user))
        if path == "/api/library":
            from motion_agent.app.product_api import current_user, library_payload

            user = current_user(SETTINGS, self.headers)
            if not user:
                return _json_response(self, 401, {"ok": False, "error": True, "message": "Sign in required"})
            return _json_response(self, 200, library_payload(SETTINGS, user))
        if path.startswith("/api/progress"):
            from motion_agent.app.product_api import current_user, user_may_view_session
            from motion_agent.app.product_api import read_progress_with_queue as read_progress

            qs = parse_qs(parsed.query or "")
            session_id = (qs.get("session_id") or [""])[0].strip()
            user = current_user(SETTINGS, self.headers)
            if session_id and not user_may_view_session(SETTINGS, user, session_id):
                return _json_response(self, 403, {"error": True, "message": "This capture is private"})
            prog = read_progress(SETTINGS, session_id) if session_id else None
            if not prog:
                return _json_response(self, 404, {"error": True, "message": "Unknown capture"})
            return _json_response(self, 200, prog)
        if path == "/api/sports":
            try:
                from motion_agent.app.product_session import sports_catalog_payload

                return _json_response(self, 200, sports_catalog_payload())
            except Exception as exc:  # noqa: BLE001
                LOG.exception("sports catalog failed")
                return _json_response(
                    self, 500, {"ok": False, "error": True, "message": str(exc), "sports": []}
                )
        if path == "/api/session":
            from motion_agent.app.library import list_captures
            from motion_agent.app.product_api import current_user, user_may_view_session

            qs = parse_qs(parsed.query or "")
            requested = (qs.get("session_id") or [None])[0]
            user = current_user(SETTINGS, self.headers)
            if requested and not user_may_view_session(SETTINGS, user, requested):
                from motion_agent.app.library import owner_of
                from motion_agent.app.product_api import capture_was_deleted

                if user and owner_of(SETTINGS.product_dir, requested) == user["user_id"] and capture_was_deleted(
                    SETTINGS, requested
                ):
                    # Old link or bookmark to a swing this account deleted: tell the UI to drop it.
                    return _json_response(
                        self, 410, {"error": True, "deleted": True, "message": "You deleted this swing."}
                    )
                return _json_response(
                    self, 403, {"error": True, "message": "That capture belongs to another account"}
                )
            if user and not requested:
                from motion_agent.app.product_session import sports_catalog_payload

                owned = list_captures(SETTINGS.product_dir, user["user_id"])

                def _has_motion(sid: str) -> bool:
                    return (DATA_DIR / "sessions" / sid / "ik" / "opensim-motion.json").is_file()

                ready = next(
                    (
                        row["session_id"]
                        for row in owned
                        if row.get("status") == "ready" and _has_motion(row["session_id"])
                    ),
                    None,
                )
                if ready is None:
                    ready = next(
                        (
                            row["session_id"]
                            for row in owned
                            if _has_motion(row["session_id"]) and row.get("status") != "error"
                        ),
                        None,
                    )
                if ready:
                    requested = ready
                elif not owned:
                    return _json_response(
                        self,
                        200,
                        {
                            "ok": True,
                            "empty": True,
                            "signed_in": True,
                            "email": user.get("email"),
                            "message": "Upload a swing to get your first fix.",
                            "product_title": "Your swing",
                            "product_subtitle": "A short side-on clip is enough. We’ll name one fix when the swing is clear.",
                            "example_questions": [],
                            "coach_briefs": [],
                            "consumer_ready": False,
                            "sports_catalog": sports_catalog_payload()["sports"],
                        },
                    )
                else:
                    requested = owned[0]["session_id"]
            if user and requested:
                owned_row = next((r for r in list_captures(SETTINGS.product_dir, user["user_id"]) if r.get("session_id") == requested), None)
                row_status = (owned_row or {}).get("status") or ""
                motion_ready = (DATA_DIR / "sessions" / requested / "ik" / "opensim-motion.json").is_file()
                if row_status in {"uploading", "extracting", "queued", "gemx", "ik", "processing"} or (
                    owned_row and not motion_ready and row_status not in {"error", "deleted", "no_swing"}
                ):
                    from motion_agent.app.product_api import read_progress
                    from motion_agent.app.product_session import sports_catalog_payload

                    prog = read_progress(SETTINGS, requested) or {}
                    return _json_response(
                        self,
                        200,
                        {
                            "ok": True,
                            "processing": True,
                            "signed_in": True,
                            "email": user.get("email"),
                            "session_id": requested,
                            "status": row_status or prog.get("stage") or "processing",
                            "message": prog.get("message")
                            or "Rebuilding your body from the video…",
                            "product_title": "Working on your swing",
                            "product_subtitle": "This usually takes a few minutes. Keep this page open.",
                            "sports_catalog": sports_catalog_payload()["sports"],
                            "consumer_ready": False,
                        },
                    )
            if not user:
                # Anonymous visitors only ever see the shared demo, never another account's upload.
                from motion_agent.app.product_api import default_visible_session_id

                requested = default_visible_session_id(SETTINGS, DATA_DIR, None)
                if not requested:
                    return _json_response(
                        self,
                        200,
                        {"ok": True, "empty": True, "demo": True, "message": "No demo capture is available yet."},
                    )
            info = get_current_session_info(DATA_DIR, session_id=requested)
            if user:
                info["signed_in"] = True
                info["email"] = user.get("email")
            else:
                info["demo"] = True
            info["backend"] = BACKEND
            info["prompt_presets"] = _public_presets()
            info["prompt_locked"] = True
            # Consumer product fields (sport-matched mode + chips). Dev UI may
            # still offer extra presets, but the session tells the truth first.
            try:
                from motion_agent.app.product_session import build_product_session_fields

                session_dir = None
                sid = info.get("session_id")
                if sid:
                    session_dir = DATA_DIR / "sessions" / str(sid)
                if session_dir is not None and session_dir.is_dir():
                    info.update(build_product_session_fields(session_dir, data_dir=DATA_DIR))
                    if user and info.get("sport") == "general":
                        info["no_swing"] = True
                        info["consumer_ready"] = False
                        if not info.get("detect_message"):
                            info["detect_message"] = (
                                "We couldn’t confirm a golf swing in this clip. "
                                "Try a side-on take that shows a full swing."
                            )
                else:
                    info.setdefault("example_questions", EXAMPLE_QUESTIONS)
                    info.setdefault("default_prompt_mode", DEFAULT_PROMPT_MODE)
            except Exception:  # noqa: BLE001
                LOG.exception("product session fields failed")
                info.setdefault("example_questions", EXAMPLE_QUESTIONS)
                info.setdefault("default_prompt_mode", DEFAULT_PROMPT_MODE)
            return _json_response(self, 200, info)
        if path == "/api/golf_phases":
            qs = parse_qs(parsed.query or "")
            video_id = (qs.get("video_id") or qs.get("session_id") or [None])[0]
            video_id, denied = _scoped_video_id(self, video_id)
            if denied is not None:
                return _json_response(self, *denied)
            max_swings = 5
            raw_max = (qs.get("max_swings") or [None])[0]
            if raw_max is not None:
                try:
                    max_swings = int(raw_max)
                except (TypeError, ValueError):
                    max_swings = 5
            payload = get_golf_phase_cards(
                video_id=video_id,
                data_dir=DATA_DIR,
                max_swings=max_swings,
            )
            code = 200 if not payload.get("error") else 404 if payload.get("type") == "NO_SESSION" else 500
            return _json_response(self, code, payload)
        if path == "/api/sport_analysis":
            qs = parse_qs(parsed.query or "")
            video_id = (qs.get("video_id") or qs.get("session_id") or [None])[0]
            video_id, denied = _scoped_video_id(self, video_id)
            if denied is not None:
                return _json_response(self, *denied)
            sport = ((qs.get("sport") or ["golf"])[0] or "golf").strip().lower()
            handedness = ((qs.get("handedness") or ["right"])[0] or "right").strip().lower()
            payload = get_sport_analysis(
                sport,
                video_id=video_id,
                data_dir=DATA_DIR,
                handedness=handedness,
            )
            code = 200 if not payload.get("error") else 404 if payload.get("type") == "NO_SESSION" else 500
            return _json_response(self, code, payload)
        if path == "/api/coach_briefs":
            qs = parse_qs(parsed.query or "")
            video_id = (qs.get("video_id") or qs.get("session_id") or [None])[0]
            video_id, denied = _scoped_video_id(self, video_id)
            if denied is not None:
                return _json_response(self, *denied)
            sport = ((qs.get("sport") or ["golf"])[0] or "golf").strip().lower()
            try:
                from motion_agent.app.coach_briefs import build_coach_briefs

                payload = build_coach_briefs(
                    video_id=video_id, data_dir=DATA_DIR, sport=sport
                )
            except Exception as exc:  # noqa: BLE001
                LOG.exception("coach briefs failed")
                payload = {"error": True, "message": str(exc), "briefs": []}
            code = 200 if not payload.get("error") else 404 if payload.get("type") == "NO_SESSION" else 500
            return _json_response(self, code, payload)
        if path == "/api/smplx_mesh_status":
            qs = parse_qs(parsed.query or "")
            video_id = (qs.get("video_id") or qs.get("session_id") or [None])[0]
            video_id, denied = _scoped_video_id(self, video_id)
            if denied is not None:
                return _json_response(self, *denied)
            session_dir = DATA_DIR / "sessions" / str(video_id)
            if session_dir is None or not session_dir.is_dir():
                return _json_response(
                    self,
                    404,
                    {
                        "error": True,
                        "type": "NO_SESSION",
                        "message": "No preprocessed video session found.",
                        "fallback": "raw_video",
                    },
                )
            return _json_response(self, 200, smplx_mesh_status(session_dir))
        if path == "/api/smplx_mesh_clip":
            qs = parse_qs(parsed.query or "")
            video_id = (qs.get("video_id") or qs.get("session_id") or [None])[0]
            video_id, denied = _scoped_video_id(self, video_id)
            if denied is not None:
                return _json_response(self, *denied)
            try:
                t0 = float((qs.get("t0") or qs.get("start") or ["0"])[0])
                t1 = float((qs.get("t1") or qs.get("end") or ["0"])[0])
            except (TypeError, ValueError):
                return _json_response(
                    self,
                    400,
                    {
                        "error": True,
                        "fallback": "raw_video",
                        "message": "t0 and t1 must be numbers",
                    },
                )
            session_dir = DATA_DIR / "sessions" / str(video_id)
            if session_dir is None or not session_dir.is_dir():
                return _json_response(
                    self,
                    404,
                    {
                        "error": True,
                        "type": "NO_SESSION",
                        "fallback": "raw_video",
                        "message": "No preprocessed video session found.",
                    },
                )
            raw_max = (qs.get("max_frames") or [None])[0]
            max_frames = 90
            if raw_max is not None:
                try:
                    max_frames = max(12, min(180, int(raw_max)))
                except (TypeError, ValueError):
                    max_frames = 90
            muscles_flag = (qs.get("muscles") or ["1"])[0]
            include_muscles = str(muscles_flag).lower() not in ("0", "false", "no", "off")
            payload = get_smplx_mesh_clip(
                session_dir,
                t0,
                t1,
                max_frames=max_frames,
                include_muscles=include_muscles,
            )
            # Soft failures still 200 so the UI can fall back to raw video cleanly.
            return _json_response(self, 200, payload)
        if path == "/api/system_prompt":
            password = _password_from_request(self, parsed)
            if not _check_prompt_password(password):
                return _json_response(
                    self,
                    401,
                    {
                        "error": True,
                        "locked": True,
                        "message": "Password required to view system prompts",
                    },
                )
            presets = list_prompt_presets()
            return _json_response(
                self,
                200,
                {
                    "ok": True,
                    "original": ORIGINAL_SYSTEM_PROMPT,
                    "default_prompt_mode": DEFAULT_PROMPT_MODE,
                    "presets": presets,
                    "note": (
                        "Switch prompt_mode among 'general', 'snowboarding', 'golf', and 'baseball'. "
                        "Pass system_prompt in POST /api/chat with X-Prompt-Password to override. "
                        "UI Save stores overrides per mode in the browser."
                    ),
                },
            )
        if path.startswith("/media/"):
            from motion_agent.app.product_api import current_user, media_allowed

            parts = path.strip("/").split("/")
            if len(parts) >= 3:
                session_id = parts[1]
                from motion_agent.app.media_urls import PARAM, token_valid
                from motion_agent.app.product_api import media_session_live

                token = (parse_qs(parsed.query or "").get(PARAM) or [None])[0]
                # A signed URL proves the server already checked this caller; still refuse
                # captures that were deleted after the URL was handed out.
                signed = token_valid(SETTINGS.product_dir, session_id, token) and media_session_live(
                    SETTINGS, session_id
                )
                if not signed and not media_allowed(SETTINGS, current_user(SETTINGS, self.headers), session_id):
                    return _json_response(self, 403, {"error": True, "message": "This capture is private"})
                rel = "/".join(parts[2:])
                cand = resolve_media_file(session_id, rel, DATA_DIR)
                if cand is not None:
                    return self._serve_file(cand, private=True)
            return _json_response(self, 404, {"error": True, "message": "media not found"})

        _json_response(self, 404, {"error": True, "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/api/auth/register", "/api/auth/login", "/api/auth/logout"):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return _json_response(self, 400, {"error": True, "message": "invalid JSON"})
            from motion_agent.app.product_api import handle_auth_post

            code, payload = handle_auth_post(SETTINGS, parsed.path, body, self.headers)
            return _json_response(self, code, payload)
        if parsed.path == "/api/upload":
            from motion_agent.app.product_api import current_user, parse_upload

            user = current_user(SETTINGS, self.headers)
            if not user:
                return _json_response(self, 401, {"ok": False, "error": True, "message": "Sign in to upload"})
            try:
                code, payload = parse_upload(self, SETTINGS, user)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("upload failed")
                return _json_response(self, 400, {"ok": False, "error": True, "message": str(exc)})
            return _json_response(self, code, payload)
        if parsed.path == "/api/account/video-consent":
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16_384:
                return _json_response(self, 413, {"error": True, "message": "body too large"})
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response(self, 400, {"error": True, "message": "invalid JSON"})
            from motion_agent.app.product_api import current_user, handle_video_consent

            user = current_user(SETTINGS, self.headers)
            if not user:
                return _json_response(self, 401, {"error": True, "message": "Sign in required"})
            code, payload = handle_video_consent(SETTINGS, user, body if isinstance(body, dict) else {})
            return _json_response(self, code, payload)
        if parsed.path == "/api/account/delete":
            from motion_agent.app.product_api import current_user, remove_account

            user = current_user(SETTINGS, self.headers)
            if not user:
                return _json_response(self, 401, {"error": True, "message": "Sign in required"})
            code, payload = remove_account(SETTINGS, user)
            return _json_response(self, code, payload)
        if parsed.path == "/api/library/delete":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return _json_response(self, 400, {"error": True, "message": "invalid JSON"})
            from motion_agent.app.product_api import current_user, remove_capture

            user = current_user(SETTINGS, self.headers)
            if not user:
                return _json_response(self, 401, {"error": True, "message": "Sign in required"})
            code, payload = remove_capture(SETTINGS, user, str(body.get("session_id") or ""))
            return _json_response(self, code, payload)
        if parsed.path == "/api/stripe/webhook":
            from motion_agent.app.billing import BillingError, handle_webhook
            from motion_agent.app.product_api import record_event

            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                return _json_response(self, 400, {"error": True, "message": "bad body"})
            raw = self.rfile.read(length)
            try:
                result = handle_webhook(SETTINGS.product_dir, raw, self.headers.get("Stripe-Signature"))
            except BillingError as exc:
                return _json_response(self, exc.status, {"error": True, "message": exc.message})
            except Exception as exc:  # noqa: BLE001
                return _json_response(self, 500, _internal_error(exc, "stripe webhook"))
            if result.get("user_id"):
                record_event(SETTINGS, "billing_" + str(result.get("type")), user_id=result["user_id"])
            return _json_response(self, 200, {"ok": True})
        if parsed.path in ("/api/billing/checkout", "/api/billing/portal", "/api/events"):
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16_384:
                return _json_response(self, 413, {"error": True, "message": "body too large"})
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response(self, 400, {"error": True, "message": "invalid JSON"})
            if not isinstance(body, dict):
                body = {}
            from motion_agent.app.product_api import current_user, record_client_event, record_event

            user = current_user(SETTINGS, self.headers)
            if parsed.path == "/api/events":
                code, payload = record_client_event(SETTINGS, user, body)
                return _json_response(self, code, payload)
            if not user:
                return _json_response(self, 401, {"error": True, "message": "Sign in required"})
            from motion_agent.app.accounts import profile
            from motion_agent.app.billing import BillingError, create_checkout, create_portal

            try:
                if parsed.path == "/api/billing/checkout":
                    full_user = {**user, **profile(SETTINGS.product_dir, user["user_id"])}
                    plan = str(body.get("plan") or "annual")
                    result = create_checkout(SETTINGS.product_dir, full_user, plan)
                    record_event(SETTINGS, "checkout_created", user_id=user["user_id"], plan=plan)
                else:
                    result = create_portal(SETTINGS.product_dir, user)
            except BillingError as exc:
                return _json_response(self, exc.status, {"error": True, "message": exc.message})
            except Exception as exc:  # noqa: BLE001
                return _json_response(self, 500, _internal_error(exc, "billing"))
            return _json_response(self, 200, result)
        if parsed.path not in ("/api/chat", "/api/chat/stream"):
            return _json_response(self, 404, {"error": True, "message": "not found"})
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return _json_response(self, 400, {"error": True, "message": "invalid JSON"})

        question = (body.get("question") or "").strip()
        if not question:
            return _json_response(self, 400, {"error": True, "message": "question required"})
        video_id = body.get("video_id")
        from motion_agent.app.product_api import current_user

        user = current_user(SETTINGS, self.headers)
        if video_id:
            from motion_agent.app.product_api import media_allowed

            if not media_allowed(SETTINGS, user, str(video_id)):
                return _json_response(
                    self, 403, {"error": True, "message": "That capture belongs to another account"}
                )
        else:
            # Without an explicit capture the agent would read the globally newest session.
            from motion_agent.app.product_api import default_visible_session_id

            video_id = default_visible_session_id(SETTINGS, DATA_DIR, user)
            if not video_id:
                return _json_response(
                    self, 404, {"error": True, "type": "NO_SESSION", "message": "No capture available yet."}
                )
        # Sending video frames to the language model is off unless the operator opts in:
        # the coach answers from tool outputs, and user video should not leave this server.
        attach_video = bool(body.get("attach_video", True)) and _env_flag("CEREBEL_LLM_ATTACH_VIDEO")
        dev_backend = (self.headers.get("X-Motion-Dev") or "").strip() == "1" and _check_prompt_password(
            _password_from_request(self, parsed)
        )
        backend = (body.get("backend") if dev_backend else None) or BACKEND
        from motion_agent.app import billing as _billing

        if not dev_backend:
            pro = bool(user) and _billing.is_pro(SETTINGS.product_dir, user["user_id"])
            if user:
                key = "u:" + str(user["user_id"])
            else:
                # Behind Cloudflare Tunnel every request comes from 127.0.0.1; use the visitor IP header.
                ip = (self.headers.get("CF-Connecting-IP") or self.client_address[0] or "unknown").strip()
                key = "ip:" + ip[:64]
            allowed, _left = _billing.take_question(
                SETTINGS.product_dir, key, _billing.daily_question_limit(pro, bool(user))
            )
            if not allowed:
                message = (
                    "You've reached today's question limit. Create a free account to keep asking."
                    if not user
                    else "You've reached today's question limit. It resets at midnight UTC."
                )
                return _json_response(self, 429, {"error": True, "limit": True, "message": message})
        try:
            from motion_agent.app.product_api import record_event

            record_event(SETTINGS, "chat_question", user_id=(user or {}).get("user_id"), session_id=str(video_id))
        except Exception:  # noqa: BLE001
            LOG.exception("chat event failed")
        system_prompt = body.get("system_prompt")
        if isinstance(system_prompt, str):
            system_prompt = system_prompt.strip() or None
        else:
            system_prompt = None
        prompt_mode = body.get("prompt_mode")
        if isinstance(prompt_mode, str):
            prompt_mode = prompt_mode.strip().lower() or None
        else:
            prompt_mode = None

        # Consumer honesty: clamp to the Sport Pack this capture supports.
        # ?dev=1 sends X-Motion-Dev: 1 so playground modes still work (except mismatch).
        try:
            from motion_agent.app.product_session import resolve_prompt_mode_for_session

            dev_header = (self.headers.get("X-Motion-Dev") or "").strip() == "1"
            pw = body.get("prompt_password") if isinstance(body.get("prompt_password"), str) else None
            if not pw:
                pw = _password_from_request(self, parsed)
            allow_dev = dev_header or (bool(system_prompt) and _check_prompt_password(pw))
            prompt_mode, _mode_meta = resolve_prompt_mode_for_session(
                video_id=str(video_id) if video_id else None,
                data_dir=DATA_DIR,
                requested=prompt_mode,
                allow_dev_modes=allow_dev,
            )
        except Exception:  # noqa: BLE001
            LOG.exception("prompt_mode clamp failed")

        if system_prompt is not None:
            password = body.get("prompt_password")
            if not isinstance(password, str):
                password = _password_from_request(self, parsed)
            elif not password.strip():
                password = _password_from_request(self, parsed)
            else:
                password = password.strip()
            if not _check_prompt_password(password):
                return _json_response(
                    self,
                    401,
                    {
                        "error": True,
                        "locked": True,
                        "message": "Password required to override system prompt",
                    },
                )

        stream = parsed.path == "/api/chat/stream"
        if stream:
            return self._chat_stream(
                question=question,
                video_id=video_id,
                attach_video=attach_video,
                backend=backend,
                system_prompt=system_prompt,
                prompt_mode=prompt_mode,
            )

        try:
            vlm = build_vlm(backend)
            result = answer_question(
                question,
                video_id=video_id,
                data_dir=DATA_DIR,
                vlm=vlm,
                attach_video=attach_video,
                system_prompt=system_prompt,
                prompt_mode=prompt_mode,
            )
            return _json_response(self, 200, chat_payload(result))
        except Exception as exc:  # noqa: BLE001
            return _json_response(self, 500, _internal_error(exc, "chat"))

    def _chat_stream(
        self,
        *,
        question: str,
        video_id: str | None,
        attach_video: bool,
        backend: str,
        system_prompt: str | None,
        prompt_mode: str | None = None,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        _send_cors_headers(self)
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(event: dict) -> None:
            self.wfile.write(sse_pack(event))
            self.wfile.flush()

        try:
            vlm = build_vlm(backend)
            result = answer_question(
                question,
                video_id=video_id,
                data_dir=DATA_DIR,
                vlm=vlm,
                attach_video=attach_video,
                system_prompt=system_prompt,
                prompt_mode=prompt_mode,
                on_event=emit,
            )
            # Ensure a terminal done event even if the agent already emitted one.
            emit(
                {
                    "type": "done",
                    "answer": result.answer,
                    "session_id": result.session_id,
                    "tool_calls_used": result.tool_calls_used,
                    "error": result.error,
                    "trace": trace_to_dict(result),
                    "interim": False,
                }
            )
        except Exception as exc:  # noqa: BLE001
            public = _internal_error(exc, "chat stream")
            try:
                from motion_agent.app.product_api import record_event

                record_event(
                    SETTINGS,
                    "chat_error",
                    session_id=video_id or "",
                    message=str(exc)[:300],
                    error_id=public["error_id"],
                )
            except Exception:  # noqa: BLE001
                LOG.exception("failed to record chat error")
            try:
                emit(
                    {
                        "type": "error",
                        "message": public["message"],
                        "error_id": public["error_id"],
                        "interim": False,
                    }
                )
                emit(
                    {
                        "type": "done",
                        "answer": public["message"],
                        "session_id": video_id or "",
                        "tool_calls_used": 0,
                        "error": public["message"],
                        "interim": False,
                    }
                )
            except Exception:  # noqa: BLE001
                LOG.exception("failed to write stream error event")
        finally:
            try:
                self.close_connection = True
                self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass

    def _serve_file(self, path: Path, *, private: bool = False) -> None:
        if not path.is_file():
            return _json_response(self, 404, {"error": True, "message": f"missing {path.name}"})
        size = path.stat().st_size
        mime, _ = mimetypes.guess_type(str(path))
        if path.suffix.lower() == ".js" or path.name.endswith(".module.js"):
            mime = "text/javascript; charset=utf-8"
        # iOS Safari will not play a <video> unless Range requests get 206 answers.
        start, end = 0, size - 1
        rng = _parse_range(self.headers.get("Range"), size)
        if rng == "invalid":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            _send_cors_headers(self)
            self.end_headers()
            return
        if rng:
            start, end = rng
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        _send_cors_headers(self)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, no-cache" if private else "no-cache")
        if private:
            self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def _parse_range(header: str | None, size: int):
    """Return (start, end) for a single 'bytes=' range, None for no range, 'invalid' for 416."""
    if not header or not header.startswith("bytes=") or "," in header or size <= 0:
        return None
    first, _, last = header[6:].strip().partition("-")
    try:
        if first == "":
            n = int(last)
            if n <= 0:
                return "invalid"
            return max(0, size - n), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or end < start:
        return "invalid"
    return start, min(end, size - 1)


def main() -> None:
    try:
        from cerebel_config import apply_cli_flags, fake_egocentric_enabled
    except ImportError:
        apply_cli_flags = None
        fake_egocentric_enabled = lambda: False  # noqa: E731
    if apply_cli_flags is not None:
        remaining = apply_cli_flags()
        if remaining:
            raise SystemExit(f"unknown arguments: {' '.join(remaining)}")
    logging.basicConfig(
        level=os.environ.get("CEREBEL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    session = newest_ready_session_dir(DATA_DIR)
    LOG.info(
        "motion agent chat server on %s:%s backend=%s session=%s fake_egocentric=%s",
        HOST,
        PORT,
        BACKEND,
        session,
        fake_egocentric_enabled(),
    )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
