"""Shared structured tool / API errors."""

from __future__ import annotations

from typing import Any


def tool_error(error_type: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": True,
        "type": error_type,
        "message": message,
    }
    payload.update(extra)
    return payload
