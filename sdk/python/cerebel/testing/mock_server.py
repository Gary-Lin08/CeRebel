"""MockCerebelServer: an in-process fake of the product API for SDK tests and offline development.

Answers with the same shapes as motion_agent/app/chat_server.py, including the full upload
journey (extracting → queued → gemx → ik → ready), without a GPU, a model or real video.
The outcome of an upload follows its file name: one containing "noswing" ends in `no_swing`,
"fail" ends in `error`, "short" is rejected by the quality check; anything else is `ready`.

    with MockCerebelServer() as mock:
        client = CerebelClient(mock.url)
"""

from __future__ import annotations

import copy
import json
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

CONSENT_VERSION = "2026-10-24-us"
STAGES = (("extracting", 12), ("queued", 22), ("gemx", 45), ("ik", 78))

FINDING = {
    "id": "trail-knee",
    "title": "Trail knee straightens",
    "headline": "Your back knee locks too early",
    "plain": "On the way to the top, your back (right) knee straightens instead of staying flexed.",
    "detail": "Right-knee flexion drops from about 66° to 0° in the backswing.",
    "series": "trail-knee",
    "start_time": 0.3,
    "end_time": 1.8,
    "metric": {"value": "66° → 0°", "caption": "back-knee bend, address → top"},
    "why": "When the back knee locks, the hips stop turning and the arms have to do the work.",
    "cue": "Keep a soft back knee to the top",
    "drill": "Feet-together backswings: ten slow backswings keeping the trail knee bent.",
    "check": "Trail knee flex at the top stays near 66° instead of falling to 0°.",
    "strength": False,
    "severity": 70.0,
    "fault_id": "fault_setup_locked_legs",
}
PHASES = [
    {"id": pid, "label": label, "start_time": t0, "end_time": t1}
    for pid, label, t0, t1 in (
        ("address", "Address", 0.0, 0.3),
        ("takeaway", "Takeaway", 0.3, 0.8),
        ("backswing", "Backswing", 0.8, 1.8),
        ("top", "Top", 1.8, 1.9),
        ("downswing", "Downswing", 1.9, 2.1667),
        ("impact", "Impact", 2.1667, 2.25),
        ("follow_through", "Follow-through", 2.25, 3.0),
    )
]
SPORTS = [
    {"id": "golf", "label": "Golf", "status": "live", "blurb": "Swing phases, body fixes, and instant drills.",
     "example_questions": ["What's the biggest thing I should fix in this swing?"], "prompt_mode": "golf",
     "featured": True, "locked": False},
    {"id": "general", "label": "Human motion", "status": "live", "blurb": "General body coaching.",
     "example_questions": ["What is the person doing in this clip?"], "prompt_mode": "general",
     "featured": False, "locked": False},
]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


class _State:
    def __init__(self, stage_seconds: float, gpu: str, free_monthly: int) -> None:
        self.lock = threading.Lock()
        self.stage_seconds = stage_seconds
        self.gpu = gpu
        self.free_monthly = free_monthly
        self.accounts: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, str] = {}
        self.captures: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.questions: list[str] = []

    def user(self, headers: Any) -> dict[str, Any] | None:
        auth = str(headers.get("Authorization") or headers.get("X-User-Token") or "").strip()
        if auth.lower().startswith("bearer "):
            auth = auth[7:].strip()
        email = self.tokens.get(auth)
        return self.accounts.get(email) if email else None

    def progress(self, sid: str) -> dict[str, Any]:
        cap = self.captures[sid]
        if cap["final"] in ("rejected",):
            return {"session_id": sid, "stage": "rejected", "percent": 100, "message": cap["message"]}
        elapsed = time.monotonic() - cap["started"]
        step = int(elapsed / self.stage_seconds) if self.stage_seconds > 0 else len(STAGES) + 2
        if step < len(STAGES):
            stage, pct = STAGES[step]
            out = {"session_id": sid, "stage": stage, "percent": pct, "message": "Rebuilding your body from the video…"}
            if stage == "queued":
                out.update({"jobs_ahead": 1, "running": False, "eta_minutes": 3, "queue_message": "1 swing ahead of you — about 3 min."})
            return out
        if step == len(STAGES):
            # Same window as the real server: the pipeline says ready before sport detection stamps it.
            return {"session_id": sid, "stage": "ready", "percent": 100, "message": "Processing complete"}
        final = cap["final"]
        cap["status"] = final
        if final == "ready":
            return {"session_id": sid, "stage": "ready", "percent": 100, "message": "Detected Golf.",
                    "sport": "golf", "sport_confidence": 0.82, "consumer_ready": True}
        if final == "no_swing":
            return {"session_id": sid, "stage": "no_swing", "percent": 100, "sport": "general",
                    "message": "We rebuilt the body but couldn’t confirm a golf swing. Try a side-on clip that shows a full swing."}
        return {"session_id": sid, "stage": "error", "percent": 100,
                "message": "Something went wrong on our side while analyzing this video. Please try again."}


class _Handler(BaseHTTPRequestHandler):
    server: "MockCerebelServer"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        pass

    @property
    def st(self) -> _State:
        return self.server.state

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _visible(self, user: dict[str, Any] | None, sid: str) -> bool:
        if sid == self.server.demo_session_id:
            return True
        cap = self.st.captures.get(sid)
        return bool(user and cap and cap["user_id"] == user["user_id"] and not cap["deleted"])

    def _library(self, user: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for sid, cap in self.st.captures.items():
            if cap["user_id"] != user["user_id"] or cap["deleted"]:
                continue
            prog = self.st.progress(sid) if cap["final"] != "rejected" else None
            rows.append({"user_id": user["user_id"], "session_id": sid, "filename": cap["filename"],
                         "status": cap["status"], "created_at": cap["created_at"], "deleted": False, "progress": prog})
        rows.reverse()
        used = sum(1 for c in self.st.captures.values() if c["user_id"] == user["user_id"] and c["final"] == "ready")
        ready = [r["session_id"] for r in rows if r["status"] == "ready"]
        return {
            "ok": True,
            "user": {"user_id": user["user_id"], "email": user["email"]},
            "captures": rows,
            "compare": {"default": {"before": ready[1], "after": ready[0]} if len(ready) >= 2 else None, "any_pair": False},
            "quota": {"plan": "free", "monthly_limit": self.st.free_monthly, "used_this_month": used,
                      "remaining": max(0, self.st.free_monthly - used), "resets": "the 1st of each month (UTC)"},
            "subscription": {"pro": False, "status": None, "plan": None, "current_period_end": None,
                             "cancel_at_period_end": False, "can_manage": False},
            "billing": {"checkout_enabled": True, "plans": [{"id": "annual", "label": "Pro annual", "price": "$99.99/year"}],
                        "free_monthly": self.st.free_monthly, "pro_monthly": 60},
            "video_consent": self._consent(user),
        }

    def _consent(self, user: dict[str, Any]) -> dict[str, Any]:
        accepted = user.get("consent_version")
        return {"version": CONSENT_VERSION, "required": accepted != CONSENT_VERSION, "accepted_version": accepted,
                "accepted_at": user.get("consent_at"), "text": "I agree that CeRebel may process the videos I upload (mock text).",
                "raw_video_retention_days": 30, "inactive_delete_days": 730}

    # ---------------------------------------------------------------- GET

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query or "")
        q = lambda k, d=None: (qs.get(k) or [d])[0]  # noqa: E731
        user = self.st.user(self.headers)
        with self.st.lock:
            if path == "/api/health":
                ok = self.st.gpu != "down"
                return self._json(200 if ok else 503, {"ok": ok, "process": "up", "backend": "mock",
                                                       "gpu": {"ok": ok, "state": self.st.gpu}})
            if path == "/api/sports":
                return self._json(200, {"ok": True, "sports": SPORTS, "live_count": 2})
            if path == "/api/billing":
                lib = self._library(user) if user else None
                billing = {"checkout_enabled": True, "plans": [], "free_monthly": self.st.free_monthly, "pro_monthly": 60}
                if not lib:
                    return self._json(200, {"ok": True, "user": None, "billing": billing})
                return self._json(200, {k: lib[k] for k in ("ok", "user", "quota", "subscription", "billing")})
            if path in ("/api/me", "/api/library"):
                if not user:
                    if path == "/api/me":
                        return self._json(200, {"ok": True, "user": None, "captures": []})
                    return self._json(401, {"ok": False, "error": True, "message": "Sign in required"})
                return self._json(200, self._library(user))
            if path == "/api/progress":
                sid = str(q("session_id", "") or "")
                if sid == self.server.demo_session_id:
                    return self._json(200, {"session_id": sid, "stage": "ready", "percent": 100, "message": "Ready to coach"})
                if not self._visible(user, sid):
                    return self._json(403, {"error": True, "message": "This capture is private"})
                return self._json(200, self.st.progress(sid))
            if path in ("/api/session", "/api/sport_analysis", "/api/golf_phases", "/api/coach_briefs", "/api/compare"):
                return self._report(path, user, q)
            if path.startswith("/media/"):
                parts = path.strip("/").split("/")
                if len(parts) >= 3 and (self._visible(user, parts[1]) or q("mt")):
                    raw = b"\x00\x00\x00\x18ftypmp42mock-video"
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return None
                return self._json(403, {"error": True, "message": "This capture is private"})
        return self._json(404, {"error": True, "message": "not found"})

    def _report(self, path: str, user: dict[str, Any] | None, q: Any) -> None:
        if path == "/api/compare":
            if not user:
                return self._json(401, {"ok": False, "error": True, "message": "Sign in required"})
            before, after = q("before"), q("after")
            if not before or not after or before == after:
                return self._json(400, {"ok": False, "error": True, "message": "Pick two swings to compare."})
            if not (self._visible(user, before) and self._visible(user, after)):
                return self._json(403, {"ok": False, "error": True, "message": "This capture is private"})
            side = lambda sid: {"session_id": sid, "phases": PHASES, "main_issue": {"finding_id": FINDING["id"], "headline": FINDING["headline"]}}  # noqa: E731
            return self._json(200, {
                "ok": True, "version": 1, "before": side(before), "after": side(after),
                "comparability": {"level": "full", "reasons": [], "message": "Film from the same spot each time for a fair comparison."},
                "metrics": [{"id": "trail_knee_loss", "label": "Back-knee bend lost by the top", "unit": "°", "before": 66.0,
                             "after": 40.0, "delta": -26.0, "display": "66° → 40°", "better": "lower", "margin": "±5°",
                             "verdict": "improved", "finding_id": FINDING["id"]}],
                "main_issue": {"finding_id": FINDING["id"], "headline": FINDING["headline"], "display": "66° → 40°",
                               "status": "improved", "status_label": "Better"},
            })
        sid = q("session_id") or q("video_id")
        if path == "/api/session" and not sid:
            if q("sample") == "1" or not user:
                sid = self.server.demo_session_id
            else:
                lib = self._library(user)
                ready = [r["session_id"] for r in lib["captures"] if r["status"] == "ready"]
                if not lib["captures"]:
                    return self._json(200, {"ok": True, "empty": True, "signed_in": True, "email": user["email"],
                                            "message": "Upload a swing to get your first fix."})
                sid = ready[0] if ready else lib["captures"][0]["session_id"]
        if not sid:
            sid = self.server.demo_session_id
        if not self._visible(user, sid):
            return self._json(403, {"error": True, "message": "That capture belongs to another account"})
        cap = self.st.captures.get(sid)
        if cap is not None:
            prog = self.st.progress(sid)
            if prog["stage"] not in ("ready", "no_swing", "error", "rejected") or (prog["stage"] == "ready" and "sport" not in prog):
                if path == "/api/session":
                    return self._json(200, {"ok": True, "processing": True, "signed_in": True, "session_id": sid,
                                            "status": prog["stage"], "stage": prog["stage"], "percent": prog["percent"],
                                            "message": prog.get("message")})
                return self._json(404, {"error": True, "type": "NO_SESSION", "message": "No preprocessed video session found."})
        if path == "/api/session":
            return self._json(200, {"session_id": sid, "sport": "golf", "sport_label": "Golf", "sport_confidence": 0.82,
                                    "consumer_ready": True, "has_video": True, "fps": 30.0, "duration_s": 3.0,
                                    "video_url_path": f"/media/{sid}/session.mp4?mt=mock",
                                    "product_title": "Your swing", "demo": user is None,
                                    "example_questions": SPORTS[0]["example_questions"]})
        if path == "/api/sport_analysis":
            return self._json(200, {"error": False, "tool": "build_sport_analysis", "sport": "golf", "session_id": sid,
                                    "title": "Swing analysis", "handedness": "right", "impact_time": 2.1667,
                                    "phases": PHASES, "series": [], "landmarks": [],
                                    "findings": [copy.deepcopy(FINDING)], "highlights": []})
        if path == "/api/golf_phases":
            return self._json(200, {"error": False, "ok": True, "sport": "golf", "session_id": sid, "swing_count": 1,
                                    "swings": [{"swing_index": 0, "start_time": 0.0, "end_time": 3.0,
                                                "impact_time": 2.1667, "confidence": 0.8, "phases": PHASES}]})
        return self._json(200, {"error": False, "sport": "golf", "session_id": sid, "briefs": [
            {"id": "biggest_fix", "question": "What's the biggest thing I should fix in this swing?",
             "answer": FINDING["headline"], "finding_id": FINDING["fault_id"], "instant": True}]})

    # ---------------------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/upload":
            return self._upload()
        if path == "/api/chat/stream":
            return self._chat_stream(self._body())
        body = self._body()
        with self.st.lock:
            user = self.st.user(self.headers)
            if path == "/api/auth/register":
                email = str(body.get("email") or "").strip().lower()
                if "@" not in email or len(str(body.get("password") or "")) < 8:
                    return self._json(400, {"ok": False, "error": True, "message": "Enter a valid email and a password of 8+ characters."})
                if body.get("accept_terms") is not True:
                    return self._json(400, {"ok": False, "error": True, "message": "Please accept the Terms and Privacy Policy, including video processing."})
                if email in self.st.accounts:
                    return self._json(400, {"ok": False, "error": True, "message": "An account with that email already exists."})
                self.st.accounts[email] = {"user_id": secrets.token_hex(8), "email": email, "password": body["password"]}
                return self._json(200, self._login(email))
            if path == "/api/auth/login":
                email = str(body.get("email") or "").strip().lower()
                row = self.st.accounts.get(email)
                if not row or row["password"] != body.get("password"):
                    return self._json(401, {"ok": False, "error": True, "message": "Email or password is incorrect."})
                return self._json(200, self._login(email))
            if path == "/api/auth/logout":
                token = str(self.headers.get("Authorization") or "")[7:].strip()
                self.st.tokens.pop(token, None)
                return self._json(200, {"ok": True})
            if path == "/api/events":
                self.st.events.append(body)
                return self._json(200, {"ok": True})
            if not user:
                if path in ("/api/chat",):
                    return self._chat(None, body)
                return self._json(401, {"error": True, "message": "Sign in required"})
            if path == "/api/account/video-consent":
                if body.get("accept") is not True:
                    return self._json(400, {"ok": False, "error": True, "message": "Tick the box to agree.", "video_consent": self._consent(user)})
                if str(body.get("version") or "") != CONSENT_VERSION:
                    return self._json(409, {"ok": False, "error": True, "message": "The consent text has changed. Reload the page and review it again.",
                                            "video_consent": self._consent(user)})
                user["consent_version"], user["consent_at"] = CONSENT_VERSION, _now()
                return self._json(200, {"ok": True, "video_consent": self._consent(user)})
            if path == "/api/account/delete":
                for cap in self.st.captures.values():
                    if cap["user_id"] == user["user_id"]:
                        cap["deleted"] = True
                self.st.accounts.pop(user["email"], None)
                self.st.tokens = {t: e for t, e in self.st.tokens.items() if e != user["email"]}
                return self._json(200, {"ok": True, "deleted": True})
            if path == "/api/library/delete":
                sid = str(body.get("session_id") or "")
                if not self._visible(user, sid) or sid == self.server.demo_session_id:
                    return self._json(404, {"ok": False, "error": True, "message": "Capture not found"})
                self.st.captures[sid]["deleted"] = True
                return self._json(200, {"ok": True, "session_id": sid})
            if path == "/api/library/practice":
                sid, fid = str(body.get("session_id") or ""), str(body.get("finding_id") or "")
                if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", fid):
                    return self._json(400, {"ok": False, "error": True, "message": "Unknown fix"})
                cap = self.st.captures.get(sid)
                if not cap or cap["user_id"] != user["user_id"]:
                    return self._json(404, {"ok": False, "error": True, "message": "Capture not found"})
                cap["practice"][fid] = cap["practice"].get(fid, 0) + 1
                return self._json(200, {"ok": True, "session_id": sid, "finding_id": fid, "count": cap["practice"][fid]})
            if path == "/api/billing/checkout":
                return self._json(200, {"ok": True, "url": "https://checkout.stripe.com/c/mock", "id": "cs_mock"})
            if path == "/api/billing/portal":
                return self._json(400, {"error": True, "message": "No subscription to manage yet."})
            if path == "/api/chat":
                return self._chat(user, body)
        return self._json(404, {"error": True, "message": "not found"})

    def _login(self, email: str) -> dict[str, Any]:
        token = secrets.token_urlsafe(24)
        self.st.tokens[token] = email
        row = self.st.accounts[email]
        return {"ok": True, "token": token, "user": {"user_id": row["user_id"], "email": email}}

    def _upload(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        with self.st.lock:
            user = self.st.user(self.headers)
            if not user:
                return self._json(401, {"ok": False, "error": True, "message": "Sign in to upload"})
            if "multipart/form-data" not in str(self.headers.get("Content-Type") or ""):
                return self._json(400, {"ok": False, "error": True, "message": "Expected a video file upload"})
            match = re.search(rb'name="file"; filename="([^"]*)"', raw)
            if not match:
                return self._json(400, {"ok": False, "error": True, "message": "Missing file"})
            if self.st.gpu == "down":
                return self._json(503, {"ok": False, "error": True, "unavailable": True,
                                        "message": "Swing analysis is offline for a few minutes. Nothing was uploaded and it didn't use one of your analyses. Please try again shortly."})
            lib = self._library(user)
            if lib["quota"]["remaining"] <= 0:
                return self._json(402, {"ok": False, "error": True, "paywall": True, "quota": lib["quota"],
                                        "message": f"You've used your {self.st.free_monthly} free analyses this month."})
            if lib["video_consent"]["required"]:
                return self._json(428, {"ok": False, "error": True, "consent_required": True, "video_consent": lib["video_consent"],
                                        "message": "Please review and agree to how your video is processed before your first upload."})
            filename = match.group(1).decode("utf-8", "replace")
            lower = filename.lower()
            sid = str(uuid.uuid4()).upper()
            final = "no_swing" if "noswing" in lower else "error" if "fail" in lower else "rejected" if "short" in lower else "ready"
            self.st.captures[sid] = {"user_id": user["user_id"], "filename": filename, "created_at": _now(),
                                     "started": time.monotonic(), "final": final, "deleted": False,
                                     "status": "rejected" if final == "rejected" else "processing", "practice": {},
                                     "message": "That clip is only 0.8s. Film the whole swing, from address to finish."}
            if final == "rejected":
                return self._json(422, {"ok": False, "error": True, "rejected": True,
                                        "message": self.st.captures[sid]["message"] + " This didn't use one of your analyses."})
            warnings = ["Filmed below 60 fps: fast moments like impact are less precise. Slow-motion works best."]
            return self._json(200, {"ok": True, "session_id": sid, "filename": filename, "bytes": len(raw),
                                    "stage": "extracting", "warnings": warnings})

    def _chat(self, user: dict[str, Any] | None, body: dict[str, Any]) -> None:
        question = str(body.get("question") or "").strip()
        if not question:
            return self._json(400, {"error": True, "message": "question required"})
        sid = str(body.get("video_id") or self.server.demo_session_id)
        if not self._visible(user, sid):
            return self._json(403, {"error": True, "message": "That capture belongs to another account"})
        self.st.questions.append(question)
        return self._json(200, {"answer": f"{FINDING['headline']}. {FINDING['cue']}.", "session_id": sid, "tool_calls_used": 1,
                                "trace": [{"kind": "tool", "name": "build_sport_analysis"}, {"kind": "final", "content": FINDING["headline"]}],
                                "error": None})

    def _chat_stream(self, body: dict[str, Any]) -> None:
        user = self.st.user(self.headers)
        sid = str(body.get("video_id") or self.server.demo_session_id)
        if not str(body.get("question") or "").strip():
            return self._json(400, {"error": True, "message": "question required"})
        if not self._visible(user, sid):
            return self._json(403, {"error": True, "message": "That capture belongs to another account"})
        answer = f"{FINDING['headline']}. {FINDING['cue']}."
        events = [
            {"type": "status", "message": "Calling model…", "session_id": sid, "interim": True},
            {"type": "tool", "kind": "tool", "name": "build_sport_analysis", "interim": True},
            {"type": "final", "kind": "final", "content": answer, "interim": False},
            {"type": "done", "answer": answer, "session_id": sid, "tool_calls_used": 1, "error": None, "interim": False},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for event in events:
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.close_connection = True


class MockCerebelServer(ThreadingHTTPServer):
    """Start with `start()` or a `with` block; `url` is the base URL to give the client.

    stage_seconds: how long each pipeline stage lasts (keep small in tests).
    gpu: "ready" or "down" (uploads then answer 503).
    """

    daemon_threads = True

    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, stage_seconds: float = 0.05,
                 gpu: str = "ready", free_monthly: int = 3, demo_session_id: str = "DEMO") -> None:
        super().__init__((host, port), _Handler)
        self.state = _State(stage_seconds, gpu, free_monthly)
        self.demo_session_id = demo_session_id
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "MockCerebelServer":
        self._thread = threading.Thread(target=self.serve_forever, name="mock-cerebel", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

    def __enter__(self) -> "MockCerebelServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
