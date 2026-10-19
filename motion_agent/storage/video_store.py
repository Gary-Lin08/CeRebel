"""Session RGB / video path helpers."""

from __future__ import annotations

from pathlib import Path

from motion_agent.motion.sequence import MotionSequence
from motion_agent.storage.motion_store import default_data_dir


def session_video_path(seq: MotionSequence) -> Path | None:
    if seq.video_path and seq.video_path.is_file():
        return seq.video_path
    return None


def jpeg_dir(seq: MotionSequence) -> Path | None:
    if seq.jpeg_dir and seq.jpeg_dir.is_dir():
        return seq.jpeg_dir
    return None


def media_url(session_id: str, rel: str = "session.mp4") -> str:
    return f"/media/{session_id}/{rel}"


def resolve_media_file(session_id: str, rel: str, data_dir: Path | None = None) -> Path | None:
    base = (data_dir or default_data_dir()) / "sessions" / session_id
    # Hide raw capture when fake-egocentric mode is on.
    try:
        from cerebel_config import fake_egocentric_enabled
    except ImportError:
        fake_egocentric_enabled = lambda: False  # noqa: E731
    if fake_egocentric_enabled() and Path(rel).name in (
        "session.mp4",
        "poster.jpg",
        "layup-original.mp4",
    ):
        return None
    candidates = [
        base / "display" / Path(rel).name,
        base / "ik" / rel,
        base / rel,
        base / "jpegs" / Path(rel).name,
    ]
    for cand in candidates:
        try:
            resolved = cand.resolve()
            if resolved.is_file() and base.resolve() in resolved.parents:
                return resolved
        except OSError:
            continue
    return None
