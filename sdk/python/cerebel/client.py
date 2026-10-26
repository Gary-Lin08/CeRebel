"""CerebelClient: one object per account and server, wrapping the product HTTP API."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib import parse as urlparse
from urllib import request as urlrequest

from cerebel._version import __version__
from cerebel._http import MultipartFile, call_json, iter_sse, open_request
from cerebel.analysis import FAILED_STAGES, TERMINAL_STAGES, main_fix
from cerebel.errors import AnalysisFailed, CerebelError

DEFAULT_BASE_URL = "https://cerebel.tech/playground"
# The pipeline writes "ready" first; sport detection then stamps `sport` (or turns it into
# "no_swing") a few seconds later. Wait this long for that stamp before accepting a bare "ready".
FINALIZE_GRACE_S = 90.0

ProgressCallback = Callable[[dict[str, Any]], None]


class CerebelClient:
    """Client for the Cerebel product API (the server in motion_agent/app/chat_server.py).

    Every method returns the decoded JSON body and raises a `CerebelError` subclass for
    error answers. Calls that need an account use `token`, set by `login`/`register` or passed in.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        *,
        timeout: float = 30.0,
        upload_timeout: float = 900.0,
        stream_timeout: float = 300.0,
        upload_base_url: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        # base_url may carry a path prefix (https://cerebel.tech/playground): it is kept on every call.
        self.base_url = base_url.rstrip("/")
        self.upload_base_url = (upload_base_url or self.base_url).rstrip("/")
        self.token = token
        self.timeout = timeout
        self.upload_timeout = upload_timeout
        # Per-read limit while streaming: a coach answer may take a while, but never forever.
        self.stream_timeout = stream_timeout
        self.user_agent = user_agent or f"cerebel-sdk-python/{__version__}"

    # ------------------------------------------------------------------ plumbing

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str, query: dict[str, Any] | None = None, *, base: str | None = None) -> str:
        url = (base or self.base_url) + path
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url += "?" + urlparse.urlencode(clean)
        return url

    def _get(self, path: str, query: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        req = urlrequest.Request(self._url(path, query), headers=self._headers(), method="GET")
        return call_json(req, self.timeout, **kw)

    def _post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body or {}).encode("utf-8")
        req = urlrequest.Request(
            self._url(path),
            data=data,
            headers=self._headers({"Content-Type": "application/json"}),
            method="POST",
        )
        return call_json(req, self.timeout)

    # ------------------------------------------------------------------ service

    def health(self) -> dict[str, Any]:
        """Readiness of disk, language model and GPU. Returns the body for 200 and 503 alike;
        check `ok` (and `gpu.state`) instead of catching an exception."""
        return self._get("/api/health", allow_status=(503,))

    def sports(self) -> dict[str, Any]:
        """Sport Pack catalog: which sports are live, beta or coming soon."""
        return self._get("/api/sports")

    def plans(self) -> dict[str, Any]:
        """Plans and, when signed in, this account's quota and subscription."""
        return self._get("/api/billing")

    # ------------------------------------------------------------------ account

    def register(self, email: str, password: str, *, accept_terms: bool, coach_code: str | None = None) -> dict[str, Any]:
        """Create an account and sign in. `accept_terms` must be True: the person has read
        the Terms and Privacy Policy (shown at /terms and /privacy)."""
        out = self._post(
            "/api/auth/register",
            {"email": email, "password": password, "accept_terms": bool(accept_terms), "coach_code": coach_code},
        )
        self.token = out.get("token") or self.token
        return out

    def login(self, email: str, password: str) -> dict[str, Any]:
        out = self._post("/api/auth/login", {"email": email, "password": password})
        self.token = out.get("token") or self.token
        return out

    def logout(self) -> dict[str, Any]:
        out = self._post("/api/auth/logout")
        self.token = None
        return out

    def me(self) -> dict[str, Any]:
        """Account, captures, quota, subscription and video-consent status (user is None when signed out)."""
        return self._get("/api/me")

    def library(self) -> dict[str, Any]:
        """Same as `me()` but answers 401 when signed out."""
        return self._get("/api/library")

    def accept_video_consent(self, version: str | None = None) -> dict[str, Any]:
        """Record agreement to the video-processing text. Show `me()["video_consent"]["text"]`
        to the person first; `version` defaults to the version the server shows now."""
        if version is None:
            version = str((self.me().get("video_consent") or {}).get("version") or "")
        return self._post("/api/account/video-consent", {"accept": True, "version": version})

    def delete_account(self) -> dict[str, Any]:
        """Delete the account and all its captures. Fails with 409 while a Pro plan still renews."""
        return self._post("/api/account/delete")

    def checkout(self, plan: str = "annual") -> dict[str, Any]:
        """Stripe Checkout link for `founding`, `annual` or `monthly`. Open `url` in a browser."""
        return self._post("/api/billing/checkout", {"plan": plan})

    def billing_portal(self) -> dict[str, Any]:
        """Stripe customer-portal link for managing or cancelling a subscription."""
        return self._post("/api/billing/portal")

    def track(self, event: str, props: dict[str, Any] | None = None, *, visitor: str | None = None) -> dict[str, Any]:
        """Record one product event (only names in the server's CLIENT_EVENTS list are kept)."""
        return self._post("/api/events", {"event": event, "props": props or {}, "visitor": visitor})

    # ------------------------------------------------------------------ captures

    def upload(
        self,
        path: str | Path,
        *,
        filename: str | None = None,
        trim: tuple[float, float] | None = None,
    ) -> dict[str, Any]:
        """Upload one video (MP4/MOV/WebM…, 1.5–30 s, ≥24 fps, short side ≥320 px).

        Returns `{"session_id", "stage", "warnings", ...}` at once; processing continues on the
        server. `trim=(start_s, end_s)` keeps only that window. Raises ConsentRequired (428),
        PaymentRequired (402), UploadRejected (422) or ServiceUnavailable (503).
        """
        fields = None
        if trim is not None:
            fields = {"trim_start": f"{float(trim[0]):.3f}", "trim_end": f"{float(trim[1]):.3f}"}
        body = MultipartFile("file", Path(path), filename=filename, fields=fields)
        req = urlrequest.Request(
            self._url("/api/upload", base=self.upload_base_url),
            data=iter(body),
            headers=self._headers({"Content-Type": body.content_type, "Content-Length": str(body.length)}),
            method="POST",
        )
        return call_json(req, self.upload_timeout)

    def progress(self, session_id: str) -> dict[str, Any]:
        """Where a capture is: stage (uploading, extracting, queued, gemx, ik, ready, no_swing, error,
        rejected), percent, message, and the queue position while it waits."""
        return self._get("/api/progress", {"session_id": session_id})

    def wait_for_analysis(
        self,
        session_id: str,
        *,
        timeout: float = 45 * 60,
        poll_interval: float = 5.0,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Poll until the capture reaches a final stage. Returns the final progress record for
        `ready`; raises AnalysisFailed for `error`, `no_swing` or `rejected`."""
        deadline = time.monotonic() + timeout
        ready_since: float | None = None
        last_seen: tuple[Any, Any] | None = None
        transient = 0
        while True:
            try:
                prog = self.progress(session_id)
                transient = 0
            except CerebelError as exc:
                # A restart or a dropped connection should not end a 10-minute wait.
                if exc.status in (0, 502, 503, 504) and transient < 5:
                    transient += 1
                    time.sleep(poll_interval)
                    continue
                raise
            stage = str(prog.get("stage") or "")
            seen = (stage, prog.get("percent"))
            if on_progress is not None and seen != last_seen:
                on_progress(prog)
            last_seen = seen
            if stage in FAILED_STAGES:
                raise AnalysisFailed(
                    str(prog.get("message") or f"Analysis ended with {stage}"), status=200, payload=prog
                )
            if stage == "ready":
                if "sport" in prog:
                    return prog
                ready_since = ready_since or time.monotonic()
                if time.monotonic() - ready_since >= FINALIZE_GRACE_S:
                    return prog
            elif stage in TERMINAL_STAGES:
                return prog
            if time.monotonic() >= deadline:
                raise CerebelError(f"Analysis of {session_id} did not finish within {timeout:.0f}s", payload=prog)
            time.sleep(poll_interval)

    def session(self, session_id: str | None = None, *, sample: bool = False) -> dict[str, Any]:
        """Session overview: sport detected, video URL, example questions, coach briefs.

        Without `session_id`: the account's newest ready capture (or the shared demo when signed out).
        `sample=True` opens the shared demo. While a capture is still processing the answer has
        `processing: true` and the waiting-screen fields instead of the report.
        """
        return self._get("/api/session", {"session_id": session_id, "sample": "1" if sample else None})

    def sport_analysis(self, session_id: str, *, sport: str = "golf", handedness: str = "right") -> dict[str, Any]:
        """The report: phases, measured traces, landmarks and `findings` (biggest fix first)."""
        return self._get("/api/sport_analysis", {"session_id": session_id, "sport": sport, "handedness": handedness})

    def golf_phases(self, session_id: str, *, max_swings: int = 5) -> dict[str, Any]:
        """Swings found in the clip and their phases (address … finish) with timestamps."""
        return self._get("/api/golf_phases", {"session_id": session_id, "max_swings": max_swings})

    def coach_briefs(self, session_id: str, *, sport: str = "golf") -> dict[str, Any]:
        """Instant answers to the standard questions, built without the language model."""
        return self._get("/api/coach_briefs", {"session_id": session_id, "sport": sport})

    def main_fix(self, session_id: str, *, sport: str = "golf", handedness: str = "right") -> dict[str, Any] | None:
        """The one fix the product leads with, or None when the report has no findings."""
        return main_fix(self.sport_analysis(session_id, sport=sport, handedness=handedness))

    def compare(
        self, before: str, after: str, *, before_swing: int = 0, after_swing: int = 0
    ) -> dict[str, Any]:
        """Two of this account's swings, phase by phase, with a verdict per metric."""
        return self._get(
            "/api/compare",
            {"before": before, "after": after, "before_swing": before_swing, "after_swing": after_swing},
        )

    def log_practice(self, session_id: str, finding_id: str) -> dict[str, Any]:
        """Mark a fix's drill as practiced once; returns the running count."""
        return self._post("/api/library/practice", {"session_id": session_id, "finding_id": finding_id})

    def delete_capture(self, session_id: str) -> dict[str, Any]:
        return self._post("/api/library/delete", {"session_id": session_id})

    def analyze(
        self,
        path: str | Path,
        *,
        sport: str = "golf",
        handedness: str = "right",
        trim: tuple[float, float] | None = None,
        on_progress: ProgressCallback | None = None,
        timeout: float = 45 * 60,
        poll_interval: float = 5.0,
    ) -> dict[str, Any]:
        """Upload, wait, and fetch the report in one call.

        Returns `{"session_id", "progress", "analysis", "main_fix"}`.
        """
        started = self.upload(path, trim=trim)
        session_id = str(started["session_id"])
        if on_progress is not None:
            on_progress({"session_id": session_id, "stage": started.get("stage"), "percent": None,
                         "warnings": started.get("warnings") or []})
        final = self.wait_for_analysis(session_id, timeout=timeout, poll_interval=poll_interval, on_progress=on_progress)
        report = self.sport_analysis(session_id, sport=sport, handedness=handedness)
        return {"session_id": session_id, "progress": final, "analysis": report, "main_fix": main_fix(report)}

    # ------------------------------------------------------------------ media

    def media_url(self, path: str) -> str:
        """Absolute URL for a `/media/...` path from a payload (already signed by the server).

        The server adds its own path prefix to these, so they are joined to the origin only.
        """
        if path.startswith(("http://", "https://")):
            return path
        parts = urlparse.urlsplit(self.base_url)
        return f"{parts.scheme}://{parts.netloc}{path}"

    def _same_origin(self, url: str) -> bool:
        def origin(u: str) -> tuple[str, str]:
            parts = urlparse.urlsplit(u)
            return parts.scheme, parts.netloc

        return origin(url) in {origin(self.base_url), origin(self.upload_base_url)}

    def download_media(self, path: str, dest: str | Path) -> Path:
        """Save a `/media/...` file (for example the session video) to `dest`."""
        dest = Path(dest)
        url = self.media_url(path)
        headers = self._headers({"Accept": "*/*"})
        if not self._same_origin(url):
            # Media links are signed; the login token is only ever sent to this API's own origins.
            headers.pop("Authorization", None)
        req = urlrequest.Request(url, headers=headers, method="GET")
        with open_request(req, self.upload_timeout) as resp, dest.open("wb") as out:
            while True:
                block = resp.read(256 * 1024)
                if not block:
                    break
                out.write(block)
        return dest

    # ------------------------------------------------------------------ coach

    def ask(self, question: str, session_id: str | None = None) -> dict[str, Any]:
        """Ask the AI coach about one capture. Every number in the answer comes from motion tools;
        `trace` lists the tools it called."""
        return self._post("/api/chat", {"question": question, "video_id": session_id})

    def ask_stream(self, question: str, session_id: str | None = None) -> Iterator[dict[str, Any]]:
        """Same as `ask`, as Server-Sent Events.

        Event `type`s: status, thinking, assistant, tool, final, error, done. The last event is
        `done` with the full `answer`.
        """
        data = json.dumps({"question": question, "video_id": session_id}).encode("utf-8")
        req = urlrequest.Request(
            self._url("/api/chat/stream"),
            data=data,
            headers=self._headers({"Content-Type": "application/json", "Accept": "text/event-stream"}),
            method="POST",
        )
        with open_request(req, self.stream_timeout) as resp:
            yield from iter_sse(resp)
