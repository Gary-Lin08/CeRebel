"""Discover newest preprocessed CeRebel sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def default_data_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data"


def load_latest_pointer(data_dir: Path | None = None) -> dict[str, Any] | None:
    data_dir = data_dir or default_data_dir()
    pointer = data_dir / "latest_mesh.json"
    if not pointer.is_file():
        return None
    try:
        return json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def newest_ready_session_dir(data_dir: Path | None = None) -> Path | None:
    """Return session directory for the newest successfully IK-preprocessed video.

    Preference order:
    1. data/latest_mesh.json when status==ready and opensim_motion exists
    2. newest session dir that has ik/opensim-motion.json
    """
    data_dir = Path(data_dir or default_data_dir())
    ptr = load_latest_pointer(data_dir)
    if ptr and ptr.get("status") == "ready" and ptr.get("session_id"):
        session_dir = data_dir / "sessions" / ptr["session_id"]
        motion = ptr.get("opensim_motion")
        if session_dir.is_dir() and motion and Path(motion).is_file():
            return session_dir
        if (session_dir / "ik" / "opensim-motion.json").is_file():
            return session_dir

    sessions_root = data_dir / "sessions"
    if not sessions_root.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for d in sessions_root.iterdir():
        if not d.is_dir():
            continue
        motion_path = d / "ik" / "opensim-motion.json"
        if motion_path.is_file():
            candidates.append((motion_path.stat().st_mtime, d))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def session_summary(session_dir: Path) -> dict[str, Any]:
    session_dir = Path(session_dir)
    ik_json = session_dir / "ik.json"
    summary: dict[str, Any] = {
        "session_id": session_dir.name,
        "session_dir": str(session_dir),
        "has_opensim": (session_dir / "ik" / "opensim-motion.json").is_file(),
        "has_dynamics": (session_dir / "ik" / "estimated-dynamics.json").is_file(),
        "has_video": (session_dir / "ik" / "session.mp4").is_file(),
        "jpeg_count": len(list((session_dir / "jpegs").glob("*.jpg")))
        if (session_dir / "jpegs").is_dir()
        else 0,
    }
    if ik_json.is_file():
        manifest = json.loads(ik_json.read_text(encoding="utf-8"))
        summary["fps"] = manifest.get("fps")
        summary["video"] = manifest.get("video")
        summary["subject_height_cm"] = manifest.get("subject_height_cm")
        summary["subject_mass_kg"] = manifest.get("subject_mass_kg")
    return summary
