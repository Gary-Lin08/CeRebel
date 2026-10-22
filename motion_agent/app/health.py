"""Process health that separates 'the server is up' from disk and model readiness."""

from __future__ import annotations

import os
from typing import Any

from motion_agent.app.config import Settings


def _model_credential(backend: str) -> dict[str, Any]:
    if backend == "mock":
        return {"ok": True, "credential": "not_required", "backend": backend}
    try:
        from motion_agent.llm.qwen_client import _load_api_key

        key = _load_api_key()
        return {"ok": bool(key), "credential": "present" if key else "missing", "backend": backend}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "credential": "missing", "backend": backend, "message": str(exc)}


def health_report(settings: Settings) -> tuple[int, dict[str, Any]]:
    data_dir = settings.data_dir
    sessions = data_dir / "sessions"
    disk_ok = data_dir.is_dir() and os.access(data_dir, os.W_OK)
    model = _model_credential(settings.backend)
    ready = disk_ok and bool(model.get("ok"))
    payload = {
        "ok": ready,
        "process": "up",
        "backend": settings.backend,
        "disk": {
            "ok": disk_ok,
            "data_dir": str(data_dir),
            "sessions_present": sessions.is_dir(),
        },
        "model": {k: model[k] for k in ("ok", "credential", "backend") if k in model},
        "prompt_edit_configured": settings.prompt_password is not None,
    }
    if not model.get("ok") and model.get("message") and settings.backend != "mock":
        payload["model"]["message"] = "credential unavailable"
    # Process is up either way. 503 means the product cannot accept work.
    return (200 if ready else 503), payload
