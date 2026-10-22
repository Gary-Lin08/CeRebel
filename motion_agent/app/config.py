"""Runtime settings for the motion product server. All values come from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from motion_agent.storage.motion_store import default_data_dir


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str
    port: int
    backend: str
    prompt_password: str | None
    max_upload_bytes: int
    invite_daily_limit: int

    @property
    def product_dir(self) -> Path:
        return self.data_dir / "product"


def load_settings() -> Settings:
    password = os.environ.get("MOTION_AGENT_PROMPT_PASSWORD")
    password = password.strip() if isinstance(password, str) and password.strip() else None
    return Settings(
        data_dir=Path(os.environ.get("CEREBEL_DATA_DIR", str(default_data_dir()))),
        host=os.environ.get("MOTION_AGENT_HOST", "0.0.0.0"),
        port=_env_int("MOTION_AGENT_PORT", 8092),
        backend=os.environ.get("MOTION_AGENT_BACKEND", "dashscope").strip() or "dashscope",
        prompt_password=password,
        max_upload_bytes=_env_int("CEREBEL_UPLOAD_MAX_BYTES", 512 * 1024 * 1024),
        invite_daily_limit=_env_int("CEREBEL_INVITE_DAILY_CAPTURES", 3),
    )
