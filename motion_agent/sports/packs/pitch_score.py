"""Conservative pitch-likeness score used by the baseball pack.

Never treat a confident golf swing as a pitch. Score alone is not enough to
go live — BaseballPack.DETECTOR_ENABLED stays False until we have labeled
pitch captures that pass the false-positive suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np


def pitch_likeness(
    session_dir: Path,
    *,
    video_id: str | None = None,
    data_dir: Path | None = None,
    handedness: str = "right",
) -> dict[str, Any]:
    """Return a 0–1 pitch likeness score and diagnostic detail."""
    from motion_agent.motion.adapter import adapt_session
    from motion_agent.motion.sequence import velocity_of
    from motion_agent.motion.synchronization import slice_indices

    sid = video_id or session_dir.name
    try:
        seq = adapt_session(session_dir)
    except Exception as exc:  # noqa: BLE001
        return {"score": 0.0, "ok": False, "message": str(exc)}

    hand = "right" if (handedness or "right").strip().lower() != "left" else "left"
    trail = hand
    lead = "left" if trail == "right" else "right"
    wrist = f"{trail}_wrist"
    ankle = f"{lead}_ankle"
    if wrist not in seq.joint_positions or ankle not in seq.joint_positions:
        return {"score": 0.0, "ok": False, "message": "Missing wrist or lead ankle"}

    dt = float(seq.metadata.get("dt") or (1.0 / max(seq.fps, 1.0)))
    sl = slice_indices(seq.timestamps, 0.0, float(seq.duration_s))
    times = seq.timestamps[sl]
    if times.size < 16:
        return {"score": 0.0, "ok": False, "message": "Clip too short"}

    wrist_speed = np.linalg.norm(velocity_of(seq.joint_positions[wrist], dt)[sl], axis=-1)
    ankle_speed = np.linalg.norm(velocity_of(seq.joint_positions[ankle], dt)[sl], axis=-1)
    release_i = int(np.argmax(wrist_speed))
    peak = float(wrist_speed[release_i])
    median = float(np.median(wrist_speed)) + 1e-6
    burst = peak / median

    # Lead foot should be quieter near release than during stride (landing-ish).
    before = ankle_speed[: max(release_i, 1)]
    foot_peak = float(np.max(before)) if before.size else 0.0
    foot_at_release = float(ankle_speed[release_i])
    stride_ratio = foot_peak / (foot_at_release + 1e-6) if foot_peak > 0 else 0.0

    # Throwing arm should dominate the opposite wrist near release.
    other = f"{lead}_wrist"
    other_peak = 0.0
    if other in seq.joint_positions:
        other_speed = np.linalg.norm(velocity_of(seq.joint_positions[other], dt)[sl], axis=-1)
        other_peak = float(np.max(other_speed))
    arm_ratio = peak / (other_peak + 1e-6) if other_peak > 0 else 1.0

    # Soft score — high bar required by the pack before enabling.
    score = 0.0
    if burst >= 4.0:
        score += 0.35
    elif burst >= 2.5:
        score += 0.15
    if stride_ratio >= 1.4:
        score += 0.35
    elif stride_ratio >= 1.1:
        score += 0.15
    if arm_ratio >= 1.25:
        score += 0.3
    elif arm_ratio >= 1.05:
        score += 0.1
    score = float(min(1.0, score))

    return {
        "score": round(score, 3),
        "ok": True,
        "detail": {
            "burst": round(burst, 3),
            "stride_ratio": round(stride_ratio, 3),
            "arm_ratio": round(arm_ratio, 3),
            "peak_wrist_mps": round(peak, 3),
            "session_id": sid,
            "trail_hand": trail,
        },
        "message": "",
    }
