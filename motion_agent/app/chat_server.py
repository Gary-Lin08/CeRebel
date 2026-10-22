"""Minimal FastAPI-free HTTP chat server for the motion reasoning demo."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import traceback
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
# Local demo fallback when MOTION_AGENT_PROMPT_PASSWORD is unset.
_LEGACY_PROMPT_PASSWORD = "20081005"


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


def _check_prompt_password(provided: str | None) -> bool:
    expected = SETTINGS.prompt_password or _LEGACY_PROMPT_PASSWORD
    return bool(provided) and provided == expected


def _password_from_request(handler: BaseHTTPRequestHandler, parsed=None) -> str | None:
    header = handler.headers.get("X-Prompt-Password")
    if header:
        return header.strip()
    if parsed is not None:
        qs = parse_qs(parsed.query or "")
        vals = qs.get("password") or qs.get("prompt_password") or []
        if vals:
            return str(vals[0])
    return None


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    raw = json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        LOG.info("%s - " + fmt, self.address_string(), *args)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
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
            title = "Terms" if path == "/terms" else "Privacy"
            body = (
                "<p>CeRebel coaching is movement feedback from your video, not a medical diagnosis.</p>"
                "<p>Videos and reconstructed motion stay on this account. Delete a capture to remove it from your library.</p>"
                "<p>Invite beta allows a small number of new captures per day.</p>"
            )
            raw = f"<!doctype html><meta charset=utf-8><title>{title}</title><h1>{title}</h1>{body}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
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
            from motion_agent.app.product_api import current_user, read_progress, user_may_view_session

            qs = parse_qs(parsed.query or "")
            session_id = (qs.get("session_id") or [""])[0].strip()
            user = current_user(SETTINGS, self.headers)
            if user and session_id and not user_may_view_session(SETTINGS, user, session_id):
                return _json_response(self, 403, {"error": True, "message": "That capture belongs to another account"})
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
            from motion_agent.app.library import list_captures, owns
            from motion_agent.app.product_api import current_user

            qs = parse_qs(parsed.query or "")
            requested = (qs.get("session_id") or [None])[0]
            user = current_user(SETTINGS, self.headers)
            if user and requested and not owns(SETTINGS.product_dir, user["user_id"], requested):
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
            info = get_current_session_info(DATA_DIR, session_id=requested if user else None)
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
                from motion_agent.storage.motion_store import newest_ready_session_dir

                session_dir = None
                sid = info.get("session_id")
                if sid:
                    session_dir = DATA_DIR / "sessions" / str(sid)
                if session_dir is None or not session_dir.is_dir():
                    session_dir = newest_ready_session_dir(DATA_DIR)
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
            if video_id and not _capture_visible(self, video_id):
                return _json_response(self, 403, {"error": True, "message": "This capture is private"})
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
            if video_id and not _capture_visible(self, video_id):
                return _json_response(self, 403, {"error": True, "message": "This capture is private"})
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
            if video_id and not _capture_visible(self, video_id):
                return _json_response(self, 403, {"error": True, "message": "This capture is private"})
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
            if video_id and not _capture_visible(self, video_id):
                return _json_response(self, 403, {"error": True, "message": "This capture is private"})
            if video_id:
                session_dir = DATA_DIR / "sessions" / str(video_id)
            else:
                session_dir = newest_ready_session_dir(DATA_DIR)
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
            if video_id and not _capture_visible(self, video_id):
                return _json_response(self, 403, {"error": True, "message": "This capture is private"})
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
            if video_id:
                session_dir = DATA_DIR / "sessions" / str(video_id)
            else:
                session_dir = newest_ready_session_dir(DATA_DIR)
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
                if not media_allowed(SETTINGS, current_user(SETTINGS, self.headers), session_id):
                    return _json_response(self, 403, {"error": True, "message": "This capture is private"})
                rel = "/".join(parts[2:])
                cand = resolve_media_file(session_id, rel, DATA_DIR)
                if cand is not None:
                    return self._serve_file(cand)
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
        attach_video = bool(body.get("attach_video", True))
        backend = body.get("backend") or BACKEND
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
            LOG.exception("chat failed")
            return _json_response(
                self,
                500,
                {
                    "error": True,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )

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
        self.send_header("Access-Control-Allow-Origin", "*")
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
            LOG.exception("chat stream failed")
            try:
                from motion_agent.app.product_api import record_event

                record_event(SETTINGS, "chat_error", session_id=video_id or "", message=str(exc)[:300])
            except Exception:  # noqa: BLE001
                LOG.exception("failed to record chat error")
            try:
                emit(
                    {
                        "type": "error",
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                        "interim": False,
                    }
                )
                emit(
                    {
                        "type": "done",
                        "answer": f"Error: {exc}",
                        "session_id": video_id or "",
                        "tool_calls_used": 0,
                        "error": str(exc),
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

    def _serve_file(self, path: Path) -> None:
        if not path.is_file():
            return _json_response(self, 404, {"error": True, "message": f"missing {path.name}"})
        data = path.read_bytes()
        mime, _ = mimetypes.guess_type(str(path))
        if path.suffix.lower() == ".js" or path.name.endswith(".module.js"):
            mime = "text/javascript; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        # Allow video seeking
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


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
