"""Minimal FastAPI-free HTTP chat server for the motion reasoning demo."""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import threading
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
VENDOR_DIR = UI_DIR / "vendor"
# Optional path prefix (e.g. "/playground") when the app is also served under the main site,
# cerebel.tech/playground, through a reverse proxy that keeps the prefix. Requests without the
# prefix keep working (playground.cerebel.tech); with it, the prefix is stripped on the way in and
# added back to the absolute URLs in the UI files and JSON on the way out.
_base = os.environ.get("CEREBEL_BASE_PATH", "").strip().strip("/")
BASE_PATH = "/" + _base if _base else ""
# The developer playground (?dev=1 UI, prompt editor, ops counts) gets its own port on 127.0.0.1,
# reached over an SSH tunnel. Once it is set, the public port serves only the customer app;
# unset keeps the single shared port.
OPERATOR_PORT = int(os.environ.get("CEREBEL_OPERATOR_PORT") or 0)
SURFACE_META = '<meta name="cerebel-surface" content="{}" />'
_PREFIXED = ("/api/", "/static/", "/media/", "/terms", "/privacy")
# Behind the main site's proxy (Vercel caps request bodies at ~4.5 MB), the video upload goes
# straight to the app's own origin instead; that origin must list the main site in
# CEREBEL_ALLOWED_ORIGINS. Empty: uploads use the same path as everything else.
UPLOAD_ORIGIN = os.environ.get("CEREBEL_UPLOAD_ORIGIN", "").strip().rstrip("/")
# Same for the live voice call: the main site's proxy does not carry WebSockets. Defaults to the
# upload origin, which already points at the app itself.
VOICE_ORIGIN = os.environ.get("CEREBEL_VOICE_ORIGIN", "").strip().rstrip("/") or UPLOAD_ORIGIN


def with_base(text: str, base: str) -> str:
    """Prefix the app's absolute URLs in HTML/JS/CSS text with the base path."""
    if not base:
        return text
    for q in ('"', "'", "`", "("):
        for prefix in _PREFIXED:
            text = text.replace(q + prefix, q + base + prefix)
        if q != "(":
            text = text.replace(q + "/" + q, q + base + "/" + q)  # links to the app's home page
    return text
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
    base = getattr(handler, "base", "")
    if isinstance(base, str) and base and b'"/media/' in raw:
        raw = raw.replace(b'"/media/', b'"' + base.encode() + b"/media/")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    _send_cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    base = ""

    def log_message(self, fmt: str, *args) -> None:
        LOG.info("%s - " + fmt, self.address_string(), *args)

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        self.base = ""
        if BASE_PATH:
            path, sep, query = self.path.partition("?")
            if path == BASE_PATH or path.startswith(BASE_PATH + "/"):
                self.base = BASE_PATH
                self.path = (path[len(BASE_PATH):] or "/") + sep + query
        return True

    def _surface(self) -> str:
        """'operator' on the developer port, 'customer' on the public port once they are split, else 'shared'."""
        if getattr(self.server, "operator", False):
            return "operator"
        return "customer" if OPERATOR_PORT else "shared"

    def _dev_allowed(self) -> bool:
        return self._surface() != "customer"

    def _ui_text(self, text: str) -> str:
        if self.base:
            text = with_base(text, self.base)
            if UPLOAD_ORIGIN:
                text = text.replace(f'"{self.base}/api/upload"', f'"{UPLOAD_ORIGIN}/api/upload"')
            if VOICE_ORIGIN:
                text = text.replace(f'"{self.base}/api/voice/live"', f'"{VOICE_ORIGIN}/api/voice/live"')
        return text

    def _send_text(self, text: str, mime: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        _send_cors_headers(self)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_index(self) -> None:
        # The page reads this tag to decide between the customer app and the developer playground.
        text = (UI_DIR / "index.html").read_text(encoding="utf-8")
        text = text.replace(SURFACE_META.format("shared"), SURFACE_META.format(self._surface()), 1)
        self._send_text(self._ui_text(text), "text/html; charset=utf-8")

    def _serve_legal(self, name: str) -> None:
        raw = _legal_html(name)
        if raw is None:
            return _json_response(self, 404, {"error": True, "message": "not found"})
        if self.base:
            raw = with_base(raw.decode("utf-8"), self.base).encode("utf-8")
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
            return self._serve_index()
        if path == "/api/voice/live":
            return self._voice_live()
        if path == "/api/voice/status":
            from motion_agent.app import billing as _billing
            from motion_agent.app import voice
            from motion_agent.app.product_api import current_user

            from motion_agent.app.speech import provider_ready

            if not voice.voice_enabled():
                return _json_response(self, 200, {"enabled": False})
            user = current_user(SETTINGS, self.headers)
            pro = bool(user) and _billing.is_pro(SETTINGS.product_dir, user["user_id"])
            # cloud: whether the server can carry the audio itself when the browser cannot.
            return _json_response(
                self, 200, {"enabled": True, "cloud": provider_ready(), **voice.voice_allowance(SETTINGS.product_dir, user, pro)}
            )
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
            # Resolve first: "/static/../../.env.product" must never leave the UI folder.
            ui_root = UI_DIR.resolve()
            target = (ui_root / path[len("/static/") :]).resolve()
            if ui_root not in target.parents:
                return _json_response(self, 404, {"error": True, "message": "not found"})
            return self._serve_file(target)
        if path == "/api/health":
            code, payload = health_report(SETTINGS)
            return _json_response(self, code, payload)
        if path in ("/api/ops", "/api/system_prompt") and not self._dev_allowed():
            return _json_response(self, 404, {"error": True, "message": "not found"})
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
        if path == "/api/compare":
            from motion_agent.app.compare import compare_payload
            from motion_agent.app.product_api import current_user

            user = current_user(SETTINGS, self.headers)
            if not user:
                return _json_response(self, 401, {"ok": False, "error": True, "message": "Sign in required"})
            qs = parse_qs(parsed.query or "")
            code, payload = compare_payload(
                SETTINGS,
                user,
                (qs.get("before") or [None])[0],
                (qs.get("after") or [None])[0],
                before_swing=(qs.get("before_swing") or [0])[0],
                after_swing=(qs.get("after_swing") or [0])[0],
            )
            return _json_response(self, code, payload)
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
            if not requested and (qs.get("sample") or [""])[0] == "1":
                # "See a sample swing" opens the shared demo even for an account with swings of its own.
                from motion_agent.app.product_api import configured_demo_session_id

                requested = configured_demo_session_id(SETTINGS)
                if not requested:
                    return _json_response(
                        self,
                        200,
                        {"ok": True, "empty": True, "demo": True, "message": "No demo capture is available yet."},
                    )
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
                    from motion_agent.app.notify import enabled as email_enabled
                    from motion_agent.app.product_api import read_progress_with_queue
                    from motion_agent.app.product_session import sports_catalog_payload

                    prog = read_progress_with_queue(SETTINGS, requested) or {}
                    emails = email_enabled()
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
                            "product_subtitle": (
                                "This usually takes a few minutes. You can close this page; we'll email you when it's ready."
                                if emails
                                else "This usually takes a few minutes. This page updates on its own."
                            ),
                            # What the waiting screen shows: the step, how far along, and the line ahead.
                            "stage": prog.get("stage") or row_status or "processing",
                            "percent": prog.get("percent"),
                            "jobs_ahead": prog.get("jobs_ahead"),
                            "eta_minutes": prog.get("eta_minutes"),
                            "queue_message": prog.get("queue_message"),
                            "email_when_ready": emails,
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
        if path == "/api/swing_keypoints":
            qs = parse_qs(parsed.query or "")
            video_id = (qs.get("video_id") or qs.get("session_id") or [None])[0]
            video_id, denied = _scoped_video_id(self, video_id)
            if denied is not None:
                return _json_response(self, *denied)
            from motion_agent.motion.swing_overlay import keypoints_payload

            pose_path = DATA_DIR / "sessions" / str(video_id) / "ik" / "pose3d.json"
            try:
                pose3d = json.loads(pose_path.read_text(encoding="utf-8")) if pose_path.is_file() else None
            except (OSError, json.JSONDecodeError):
                pose3d = None
            return _json_response(self, 200, {"session_id": video_id, **keypoints_payload(pose3d)})
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
        if parsed.path == "/api/library/practice":
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4096:
                return _json_response(self, 413, {"error": True, "message": "body too large"})
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _json_response(self, 400, {"error": True, "message": "invalid JSON"})
            from motion_agent.app.product_api import current_user, log_practice

            user = current_user(SETTINGS, self.headers)
            if not user:
                return _json_response(self, 401, {"error": True, "message": "Sign in required"})
            code, payload = log_practice(SETTINGS, user, body if isinstance(body, dict) else {})
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
        if not self._dev_allowed() and (body.get("system_prompt") or body.get("backend")):
            # Prompt overrides and backend switches belong to the developer port.
            return _json_response(self, 403, {"error": True, "message": "Not available here"})
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
        dev_backend = (
            self._dev_allowed()
            and (self.headers.get("X-Motion-Dev") or "").strip() == "1"
            and _check_prompt_password(_password_from_request(self, parsed))
        )
        backend = (body.get("backend") if dev_backend else None) or BACKEND
        if not dev_backend:
            allowed, message = self._take_question(user)
            if not allowed:
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
            # On the customer port X-Motion-Dev is ignored: any visitor can send that header.
            allow_dev = self._dev_allowed() and (dev_header or (bool(system_prompt) and _check_prompt_password(pw)))
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

    def _take_question(self, user: dict | None) -> tuple[bool, str]:
        """Count one coach question against today's cap. Returns (allowed, message for the user)."""
        from motion_agent.app import billing as _billing

        pro = bool(user) and _billing.is_pro(SETTINGS.product_dir, user["user_id"])
        if user:
            key = "u:" + str(user["user_id"])
        else:
            # Behind a proxy (Cloudflare Tunnel, Vercel, the GPU host's port mapping) every request
            # comes from a local address; use the visitor IP the proxy forwards.
            forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0]
            ip = (
                self.headers.get("CF-Connecting-IP")
                or forwarded  # first hop = the visitor; X-Real-IP is the nearest proxy
                or self.headers.get("X-Real-IP")
                or self.client_address[0]
                or "unknown"
            ).strip()
            key = "ip:" + ip[:64]
        allowed, _left = _billing.take_question(
            SETTINGS.product_dir, key, _billing.daily_question_limit(pro, bool(user))
        )
        if allowed and not user:
            # Forwarded IP headers can be forged: cap all anonymous questions together too.
            total = int(os.environ.get("CEREBEL_ANON_DAILY_TOTAL", "300"))
            allowed, _left = _billing.take_question(SETTINGS.product_dir, "anon-total", total)
        if allowed:
            return True, ""
        if not user:
            return False, "You've reached today's question limit. Create a free account to keep asking."
        return False, "You've reached today's question limit. It resets at midnight UTC."

    def _voice_live(self) -> None:
        """GET /api/voice/live: WebSocket for a live voice call with the coach (see voice.py)."""
        from motion_agent.app import billing as _billing
        from motion_agent.app import voice
        from motion_agent.app import ws as wslib
        from motion_agent.app.product_api import current_user, default_visible_session_id, media_allowed, record_event

        from motion_agent.app.speech import build_provider, provider_ready

        if not voice.voice_enabled():
            return _json_response(self, 404, {"error": True, "message": "not found"})
        # The token travels in the first message, not a cookie, but refuse foreign pages anyway.
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if origin and origin not in ALLOWED_ORIGINS and urlparse(origin).netloc != (self.headers.get("Host") or ""):
            return _json_response(self, 403, {"error": True, "message": "Not allowed"})
        sock = wslib.server_upgrade(self)
        if sock is None:
            return

        def refuse(reason: str, message: str) -> None:
            try:
                sock.send_text(json.dumps({"type": "end", "reason": reason, "message": message}))
            except (OSError, wslib.WebSocketClosed):
                pass
            sock.close()

        self.connection.settimeout(10)
        first = sock.recv()
        self.connection.settimeout(None)
        try:
            start = json.loads(first[1]) if first and first[0] == wslib.OP_TEXT else {}
        except ValueError:
            start = {}
        if start.get("type") != "start":
            return refuse("error", "Could not start the call.")
        device = start.get("speech") == "device"
        if not device and not provider_ready():
            return refuse("unavailable", "Voice isn't available in this browser. You can type your question instead.")
        token = str(start.get("token") or "").strip()
        user = current_user(SETTINGS, {"Authorization": "Bearer " + token}) if token else None
        video_id = start.get("video_id")
        if video_id and not media_allowed(SETTINGS, user, str(video_id)):
            return refuse("error", "That capture belongs to another account.")
        video_id = str(video_id or default_visible_session_id(SETTINGS, DATA_DIR, user) or "")
        if not video_id:
            return refuse("error", "Upload a swing before calling the coach.")
        pro = bool(user) and _billing.is_pro(SETTINGS.product_dir, user["user_id"])
        allowance = voice.voice_allowance(SETTINGS.product_dir, user, pro)
        if allowance["need"]:
            return refuse(allowance["need"], "Live coaching is part of Pro." if allowance["need"] == "upgrade" else "Sign in to talk with the coach.")
        prompt_mode = None
        try:
            from motion_agent.app.product_session import resolve_prompt_mode_for_session

            prompt_mode, _meta = resolve_prompt_mode_for_session(
                video_id=video_id, data_dir=DATA_DIR, requested=None, allow_dev_modes=False
            )
        except Exception:  # noqa: BLE001
            LOG.exception("prompt_mode clamp failed")

        def answer(question: str, history: list) -> object:
            return answer_question(
                question,
                video_id=video_id,
                data_dir=DATA_DIR,
                vlm=build_vlm(BACKEND),
                attach_video=False,
                prompt_mode=prompt_mode,
                history=history,
                voice=True,
            )

        def briefs() -> dict:
            if prompt_mode != "golf":
                return {}
            from motion_agent.app.coach_briefs import build_coach_briefs

            return build_coach_briefs(video_id=video_id, data_dir=DATA_DIR, sport="golf")

        provider = None
        if not device:
            try:
                provider = build_provider()
            except Exception:  # noqa: BLE001
                LOG.exception("voice provider unavailable")
                return refuse("unavailable", "Live coaching isn't available right now. You can keep typing your questions.")
        call = voice.VoiceCall(
            sock,
            provider,
            answer_fn=answer,
            seconds=allowance["seconds"],
            take_question=lambda: self._take_question(user),
            briefs_fn=briefs,
        )
        user_id = (user or {}).get("user_id")
        record_event(
            SETTINGS, "voice_call_start", user_id=user_id, session_id=video_id, plan=allowance["kind"],
            speech="device" if device else "cloud",
        )
        try:
            call.run()
        finally:
            if user_id:
                voice.record_voice_seconds(SETTINGS.product_dir, str(user_id), allowance["kind"], call.duration())
            record_event(SETTINGS, "voice_call_end", user_id=user_id, session_id=video_id, **call.summary())

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
        if path.suffix.lower() in (".js", ".mjs") or path.name.endswith(".module.js"):
            mime = "text/javascript; charset=utf-8"
        # Third-party files are served byte for byte (their loaders are full of "/" literals that
        # the base-path rewrite would break) and are versioned by folder, so browsers keep them.
        vendored = VENDOR_DIR in path.resolve().parents
        if (
            self.base
            and not vendored
            and path.suffix.lower() in (".html", ".js", ".css", ".json")
            and UI_DIR.resolve() in path.resolve().parents
        ):
            return self._send_text(self._ui_text(path.read_text(encoding="utf-8")), mime or "text/html; charset=utf-8")
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
        if vendored:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
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
    server, operator = build_servers(HOST, PORT, OPERATOR_PORT)
    if operator is not None:
        LOG.info("developer playground on 127.0.0.1:%s (public port serves the customer app only)", OPERATOR_PORT)
        threading.Thread(target=operator.serve_forever, name="operator-http", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
        server.server_close()
        if operator is not None:
            operator.server_close()


def build_servers(host: str, port: int, operator_port: int) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer | None]:
    """The public server, plus the developer server on loopback when operator_port is set."""
    if operator_port and operator_port == port:
        raise SystemExit("CEREBEL_OPERATOR_PORT must differ from MOTION_AGENT_PORT")
    public = ThreadingHTTPServer((host, port), Handler)
    public.operator = False
    if not operator_port:
        return public, None
    # Loopback only, whatever HOST is: the developer surface is never exposed directly.
    operator = ThreadingHTTPServer(("127.0.0.1", operator_port), Handler)
    operator.operator = True
    return public, operator


if __name__ == "__main__":
    main()
