"""RGB frame / clip retrieval for the motion agent."""

from __future__ import annotations

import base64
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from motion_agent.motion.sequence import MotionSequence
from motion_agent.motion.synchronization import (
    clamp_timestamp,
    frame_to_timestamp,
    timestamp_to_frame,
)
from motion_agent.storage.cache import GLOBAL_CACHE
from motion_agent.storage.errors import tool_error


def _jpeg_path(seq: MotionSequence, frame_index: int) -> Path | None:
    if not seq.jpeg_dir:
        return None
    path = seq.jpeg_dir / f"{frame_index:06d}.jpg"
    if path.is_file():
        return path
    # Some captures may be 0-based with gaps; try nearest existing
    candidates = sorted(seq.jpeg_dir.glob("*.jpg"))
    if not candidates:
        return None
    if frame_index < len(candidates):
        # Prefer numeric stem match
        by_stem = {int(p.stem): p for p in candidates if p.stem.isdigit()}
        if frame_index in by_stem:
            return by_stem[frame_index]
        keys = sorted(by_stem)
        nearest = min(keys, key=lambda k: abs(k - frame_index))
        return by_stem[nearest]
    return candidates[min(frame_index, len(candidates) - 1)]


def get_rgb_frame(seq: MotionSequence, timestamp: float) -> dict[str, Any]:
    """Retrieve the RGB frame nearest to timestamp.

    Returns base64 JPEG for VLM consumption. Numerical joint angles must NOT
    be inferred from this image when motion tools are available.
    """
    try:
        t = float(timestamp)
    except (TypeError, ValueError):
        return tool_error("INVALID_ARGUMENT", "timestamp must be a float")
    if t < -1e-6 or t > seq.duration_s + 0.05:
        return tool_error(
            "TIMESTAMP_OUT_OF_RANGE",
            f"timestamp {t} outside [0, {seq.duration_s:.4f}]",
        )
    t = clamp_timestamp(t, max(seq.duration_s - 1e-6, 0.0))
    frame_index = timestamp_to_frame(t, seq.fps, seq.frame_count)
    cache_key = GLOBAL_CACHE.make_key(
        "rgb_frame", {"video_id": seq.video_id, "frame": frame_index}
    )
    cached = GLOBAL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    path = _jpeg_path(seq, frame_index)
    if path is None:
        return tool_error(
            "MISSING_RGB_FRAME",
            f"No JPEG frame for index {frame_index}",
            frame_index=frame_index,
        )
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    result = {
        "error": False,
        "timestamp_requested": float(timestamp),
        "timestamp_actual": round(frame_to_timestamp(frame_index, seq.fps), 5),
        "frame_index": frame_index,
        "width_hint": None,
        "height_hint": None,
        "mime_type": "image/jpeg",
        "image_base64": b64,
        "path": str(path),
        "note": (
            "Use for visual appearance / scene context. "
            "Do not treat this as the source of truth for joint angles."
        ),
    }
    return GLOBAL_CACHE.set(cache_key, result)


def get_rgb_clip(
    seq: MotionSequence,
    start_time: float,
    end_time: float,
    target_fps: float | None = None,
    max_frames: int = 48,
) -> dict[str, Any]:
    """Retrieve a short RGB clip as ordered JPEG frames (base64).

    Prefer native video input to the VLM when available; use this for precise
    short temporal inspection windows.
    """
    try:
        t0, t1 = float(start_time), float(end_time)
    except (TypeError, ValueError):
        return tool_error("INVALID_ARGUMENT", "start_time/end_time must be floats")
    if t1 < t0:
        t0, t1 = t1, t0
    if t1 - t0 > 8.0:
        return tool_error(
            "CLIP_TOO_LONG",
            "Clip window exceeds 8 seconds; narrow the interval.",
            start_time=t0,
            end_time=t1,
        )
    if t0 > seq.duration_s or t1 < 0:
        return tool_error("TIMESTAMP_OUT_OF_RANGE", "clip outside video duration")

    t0 = clamp_timestamp(t0, seq.duration_s)
    t1 = clamp_timestamp(t1, seq.duration_s)
    fps = float(target_fps) if target_fps else min(seq.fps, 6.0)
    fps = max(0.5, min(fps, seq.fps))

    cache_key = GLOBAL_CACHE.make_key(
        "rgb_clip",
        {"video_id": seq.video_id, "t0": t0, "t1": t1, "fps": fps, "max": max_frames},
    )
    cached = GLOBAL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    n = max(1, int(round((t1 - t0) * fps)) + 1)
    n = min(n, max_frames)
    times = [t0 + i * (t1 - t0) / max(n - 1, 1) for i in range(n)] if n > 1 else [t0]
    frames = []
    for t in times:
        fr = get_rgb_frame(seq, t)
        if fr.get("error"):
            continue
        frames.append(
            {
                "timestamp": fr["timestamp_actual"],
                "frame_index": fr["frame_index"],
                "image_base64": fr["image_base64"],
                "mime_type": fr["mime_type"],
            }
        )
    if not frames:
        return tool_error("MISSING_RGB_FRAME", "Could not extract any frames for clip")

    result = {
        "error": False,
        "start_time": t0,
        "end_time": t1,
        "target_fps": fps,
        "frame_count": len(frames),
        "frames": frames,
        "video_path": str(seq.video_path) if seq.video_path else None,
        "note": (
            "Short temporal visual window. Prefer motion tools for numerical kinematics."
        ),
    }
    return GLOBAL_CACHE.set(cache_key, result)


def extract_video_segment_file(
    seq: MotionSequence,
    start_time: float,
    end_time: float,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Optional ffmpeg trim of session.mp4 for VLM video_url upload."""
    if not seq.video_path or not seq.video_path.is_file():
        return tool_error("MISSING_VIDEO", "session.mp4 not available")
    try:
        t0, t1 = float(start_time), float(end_time)
    except (TypeError, ValueError):
        return tool_error("INVALID_ARGUMENT", "start_time/end_time must be floats")
    if t1 <= t0:
        return tool_error("INVALID_ARGUMENT", "end_time must be > start_time")
    duration = t1 - t0
    out = output_path or Path(tempfile.mkstemp(suffix=".mp4", prefix="motion_clip_")[1])
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(t0),
        "-i",
        str(seq.video_path),
        "-t",
        str(duration),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(out),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=120)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return tool_error("VIDEO_EXTRACT_FAILED", str(exc))
    return {
        "error": False,
        "path": str(out),
        "start_time": t0,
        "end_time": t1,
        "size_bytes": out.stat().st_size if out.is_file() else 0,
    }
