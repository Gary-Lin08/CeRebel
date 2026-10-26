"""Rough camera direction for a golf capture, from the stored 2D and 3D landmarks.

The 3D body is yaw-normalised, so it cannot say where the phone stood. ``ik/pose3d.json``
keeps both the 3D landmarks and their projection into the picture, which can.

At address the hips and shoulders lie roughly along the target line. Seen from the
front (face-on) that line is parallel to the picture and shows its full width; seen
down the line it points at the lens and shrinks to almost nothing. Comparing the width
in the picture with the width in 3D (both scaled by the body's vertical extent, which
does not change with camera direction) gives |cos| of the angle between the hip line
and the picture plane:

    azimuth 0°  = face-on
    azimuth 90° = down the line

This is a coarse estimate (perspective, camera tilt and the golfer's own posture all
bend it). Use it to tell whether two clips were filmed from about the same spot, not
as a measurement.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# MediaPipe-33 indices used in pose3d.json.
_L_SHOULDER, _R_SHOULDER = 11, 12
_L_HIP, _R_HIP = 23, 24
_L_ANKLE, _R_ANKLE = 27, 28

FACE_ON_MAX_DEG = 30.0
DOWN_THE_LINE_MIN_DEG = 60.0
# Below this share of the frame height the body is too small to read a width from.
_MIN_BODY_HEIGHT_FRAC = 0.08

VIEW_LABELS = {
    "face_on": "From the front",
    "down_the_line": "Down the line",
    "angled": "At an angle",
    "unknown": "Unknown angle",
}


def unknown_view(reason: str) -> dict[str, Any]:
    return {
        "id": "unknown",
        "label": VIEW_LABELS["unknown"],
        "azimuth_deg": None,
        "known": False,
        "reason": reason,
    }


def _image_size(pose3d: dict[str, Any]) -> tuple[float, float]:
    size = (pose3d.get("metadata") or {}).get("imageSize") or []
    try:
        width, height = float(size[0]), float(size[1])
    except (IndexError, TypeError, ValueError):
        return 1.0, 1.0
    if width <= 0 or height <= 0:
        return 1.0, 1.0
    return width, height


def _frame_cos(image: np.ndarray, world: np.ndarray, width: float, height: float) -> list[float]:
    """|cos| of the hip line and the shoulder line against the picture plane, for one frame."""
    px = image[:, :2] * np.array([width, height])
    sh_2d = 0.5 * (px[_L_SHOULDER] + px[_R_SHOULDER])
    an_2d = 0.5 * (px[_L_ANKLE] + px[_R_ANKLE])
    sh_3d = 0.5 * (world[_L_SHOULDER] + world[_R_SHOULDER])
    an_3d = 0.5 * (world[_L_ANKLE] + world[_R_ANKLE])
    height_2d = abs(float(sh_2d[1] - an_2d[1]))
    height_3d = abs(float(sh_3d[1] - an_3d[1]))  # world is Y-up
    if height_3d < 0.3 or height_2d < _MIN_BODY_HEIGHT_FRAC * height:
        return []
    px_per_m = height_2d / height_3d
    out = []
    for a, b in ((_L_HIP, _R_HIP), (_L_SHOULDER, _R_SHOULDER)):
        width_3d = float(np.hypot(world[a, 0] - world[b, 0], world[a, 2] - world[b, 2]))
        if width_3d < 0.12:
            continue
        width_2d = abs(float(px[a, 0] - px[b, 0]))
        out.append(min(1.0, width_2d / (px_per_m * width_3d)))
    return out


def estimate_view(pose3d: dict[str, Any] | None, start_time: float, end_time: float) -> dict[str, Any]:
    """Camera azimuth from the frames between start_time and end_time (use the address window)."""
    if not pose3d or not isinstance(pose3d.get("frames"), list):
        return unknown_view("no_landmarks")
    width, height = _image_size(pose3d)
    frames = pose3d["frames"]
    lo, hi = float(start_time), float(end_time)
    if hi - lo < 0.05:
        hi = lo + 0.3
    picked = [f for f in frames if lo <= float(f.get("time", -1.0)) <= hi]
    if len(picked) < 3:
        picked = frames[: min(len(frames), 10)]
    samples: list[float] = []
    for frame in picked:
        try:
            image = np.asarray(frame["image"], dtype=np.float64)
            world = np.asarray(frame["world"], dtype=np.float64)
        except (KeyError, TypeError, ValueError):
            continue
        if image.shape[0] < 33 or world.shape[0] < 33 or image.shape[1] < 2 or world.shape[1] < 3:
            continue
        # Without a camera the exporter writes every image point at the centre.
        if np.allclose(image[:, :2], image[0, :2]):
            continue
        samples.extend(_frame_cos(image, world, width, height))
    if len(samples) < 3:
        return unknown_view("too_few_frames")
    cos_view = float(np.median(samples))
    azimuth = float(np.degrees(np.arccos(np.clip(cos_view, 0.0, 1.0))))
    if azimuth <= FACE_ON_MAX_DEG:
        view_id = "face_on"
    elif azimuth >= DOWN_THE_LINE_MIN_DEG:
        view_id = "down_the_line"
    else:
        view_id = "angled"
    return {
        "id": view_id,
        "label": VIEW_LABELS[view_id],
        "azimuth_deg": round(azimuth, 1),
        "known": True,
    }
