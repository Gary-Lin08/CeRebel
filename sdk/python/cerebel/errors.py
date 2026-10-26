"""Exceptions raised by the Cerebel client. Each maps to one way the API says no."""

from __future__ import annotations

from typing import Any


class CerebelError(Exception):
    """Any error answer from the API, or a transport failure (status 0)."""

    def __init__(self, message: str, *, status: int = 0, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload: dict[str, Any] = payload or {}

    @property
    def error_id(self) -> str | None:
        """Server-side reference for a 500 answer; quote it when reporting a bug."""
        return self.payload.get("error_id")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.status}, message={self.message!r})"


class AuthError(CerebelError):
    """401: not signed in, token expired (30 days by default) or wrong password."""


class PaymentRequired(CerebelError):
    """402: the monthly analysis allowance is used up. `quota` says what the plan allows."""

    @property
    def quota(self) -> dict[str, Any]:
        return self.payload.get("quota") or {}


class Forbidden(CerebelError):
    """403: the capture belongs to another account, or the action is operator-only."""


class NotFound(CerebelError):
    """404: unknown capture, endpoint, or no session to show yet."""


class Gone(CerebelError):
    """410: the account deleted this capture."""


class UploadRejected(CerebelError):
    """422: the video failed the quality check (length, fps, size). It did not use an analysis."""


class ConsentRequired(CerebelError):
    """428: the account has not agreed to the current video-processing text yet.

    Show `consent["text"]` to the person, then call `accept_video_consent(consent["version"])`.
    """

    @property
    def consent(self) -> dict[str, Any]:
        return self.payload.get("video_consent") or {}


class RateLimited(CerebelError):
    """429: today's coach-question allowance is used up (resets at midnight UTC)."""


class ServiceUnavailable(CerebelError):
    """503: the GPU that rebuilds bodies is offline. Nothing was uploaded; retry later."""


class AnalysisFailed(CerebelError):
    """The upload was accepted but its analysis ended in `error`, `no_swing` or `rejected`.

    `stage` is that final stage; `payload` is the last progress record.
    """

    @property
    def stage(self) -> str:
        return str(self.payload.get("stage") or "")


_BY_STATUS: dict[int, type[CerebelError]] = {
    401: AuthError,
    402: PaymentRequired,
    403: Forbidden,
    404: NotFound,
    410: Gone,
    422: UploadRejected,
    428: ConsentRequired,
    429: RateLimited,
    503: ServiceUnavailable,
}


def error_for(status: int, payload: Any) -> CerebelError:
    """Build the exception for an error answer."""
    body = payload if isinstance(payload, dict) else {}
    message = str(body.get("message") or f"HTTP {status}")
    cls = _BY_STATUS.get(status, CerebelError)
    return cls(message, status=status, payload=body)
