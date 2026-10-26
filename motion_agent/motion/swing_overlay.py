"""Skeleton lines drawn over the player's own video, and how each fix is marked on them.

The keypoints are the body estimate projected back into the picture (``ik/pose3d.json``
"image", normalised 0–1). They follow the reconstructed body, not a per-frame 2D detector,
so a hidden arm can be drawn slightly off the picture.

Each fix gets an ``overlay`` spec: which bones turn amber, and whether to add a joint angle,
a direction arrow, reference lines or a movement path. Sides are resolved here (lead/trail
→ left/right), so the page only draws what it is told. Arrows are only given where the
direction can be read from the 2D picture itself (a knee bending, an arm reaching, hips
moving); turning is not, so the turn fix gets lines and a number instead.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# MediaPipe-33 indices that the exporter fills from the body (heels and face points are copies).
KEYPOINTS: tuple[tuple[str, int], ...] = (
    ("nose", 0),
    ("left_shoulder", 11),
    ("right_shoulder", 12),
    ("left_elbow", 13),
    ("right_elbow", 14),
    ("left_wrist", 15),
    ("right_wrist", 16),
    ("left_hip", 23),
    ("right_hip", 24),
    ("left_knee", 25),
    ("right_knee", 26),
    ("left_ankle", 27),
    ("right_ankle", 28),
    ("left_toe", 31),
    ("right_toe", 32),
)
KEYPOINT_NAMES = tuple(name for name, _ in KEYPOINTS)

BONES: tuple[tuple[str, str], ...] = (
    ("left_shoulder", "right_shoulder"),
    ("left_shoulder", "left_elbow"),
    ("left_elbow", "left_wrist"),
    ("right_shoulder", "right_elbow"),
    ("right_elbow", "right_wrist"),
    ("left_shoulder", "left_hip"),
    ("right_shoulder", "right_hip"),
    ("left_hip", "right_hip"),
    ("left_hip", "left_knee"),
    ("left_knee", "left_ankle"),
    ("right_hip", "right_knee"),
    ("right_knee", "right_ankle"),
    ("left_ankle", "left_toe"),
    ("right_ankle", "right_toe"),
)

# Coordinates travel as integers in 1/10000 of the frame (0–10000).
SCALE = 10000


def keypoints_payload(pose3d: dict[str, Any] | None) -> dict[str, Any]:
    """Per-frame picture coordinates of the skeleton joints, or available=False."""
    empty = {"available": False, "names": list(KEYPOINT_NAMES), "bones": [list(b) for b in BONES]}
    if not pose3d or not isinstance(pose3d.get("frames"), list) or not pose3d["frames"]:
        return {**empty, "reason": "no_landmarks"}
    meta = pose3d.get("metadata") or {}
    fps = float(meta.get("fps") or 30.0)
    size = meta.get("imageSize") or []
    idx = [i for _, i in KEYPOINTS]
    frames: list[list[int] | None] = []
    usable = 0
    for frame in pose3d["frames"]:
        try:
            image = np.asarray(frame["image"], dtype=np.float64)[:, :2]
        except (KeyError, TypeError, ValueError, IndexError):
            frames.append(None)
            continue
        if image.shape[0] < 33 or not np.all(np.isfinite(image[idx])):
            frames.append(None)
            continue
        pts = image[idx]
        # Without a camera the exporter puts every point at the centre: nothing to draw.
        if np.allclose(pts, pts[0], atol=1e-6):
            frames.append(None)
            continue
        usable += 1
        frames.append([int(round(float(v) * SCALE)) for v in pts.reshape(-1)])
    if usable < 3:
        return {**empty, "reason": "no_projection"}
    times = [round(float(f.get("time", i / fps)), 4) for i, f in enumerate(pose3d["frames"])]
    return {
        "available": True,
        "fps": fps,
        "image_size": [int(size[0]), int(size[1])] if len(size) >= 2 else None,
        "scale": SCALE,
        "names": list(KEYPOINT_NAMES),
        "bones": [list(b) for b in BONES],
        "times": times,
        "frames": frames,
        "note": "Lines show the 3D body estimate drawn on your video.",
    }


def _limb(side: str, a: str, b: str) -> list[list[str]]:
    return [[f"{side}_{a}", f"{side}_{b}"]]


def overlay_for_finding(finding_id: str, series: str | None, lead: str, trail: str) -> dict[str, Any] | None:
    """How to mark one fix on the skeleton. Joint names are left/right after handedness."""
    fid = (finding_id or "").strip()
    if fid in ("trail-knee", "back-leg"):
        return {
            "segments": _limb(trail, "hip", "knee") + _limb(trail, "knee", "ankle"),
            "angle": {"at": f"{trail}_knee", "from": f"{trail}_hip", "to": f"{trail}_ankle",
                      "series": "trail-knee" if fid == "trail-knee" else "back-knee"},
            "arrow": {"kind": "bend", "at": f"{trail}_knee", "from": f"{trail}_hip", "to": f"{trail}_ankle",
                      "label": "Keep this bend"},
        }
    if fid == "front-knee":
        return {
            "segments": _limb(lead, "hip", "knee") + _limb(lead, "knee", "ankle"),
            "angle": {"at": f"{lead}_knee", "from": f"{lead}_hip", "to": f"{lead}_ankle", "series": "front-knee"},
        }
    if fid == "lead-arm":
        return {
            "segments": _limb(lead, "shoulder", "elbow") + _limb(lead, "elbow", "wrist"),
            "angle": {"at": f"{lead}_elbow", "from": f"{lead}_shoulder", "to": f"{lead}_wrist", "series": "lead-elbow"},
            "arrow": {"kind": "extend", "from": f"{lead}_shoulder", "to": f"{lead}_wrist", "label": "Keep the arm long"},
        }
    if fid == "separation":
        return {
            "segments": [["left_shoulder", "right_shoulder"]],
            "lines": [
                {"a": "left_shoulder", "b": "right_shoulder", "role": "fix"},
                {"a": "left_hip", "b": "right_hip", "role": "reference"},
            ],
            "readout": {"series": "separation", "label": "Shoulders past hips", "absolute": True},
        }
    if fid == "early-extension":
        return {
            "segments": [["left_hip", "right_hip"]],
            "path": {"points": ["left_hip", "right_hip"], "label": "Hips stay back on this line"},
        }
    # A plain reading (no fault rule fired): light up the body part its trace measures.
    by_series = {
        "trail-knee": _limb(trail, "hip", "knee") + _limb(trail, "knee", "ankle"),
        "back-knee": _limb(trail, "hip", "knee") + _limb(trail, "knee", "ankle"),
        "front-knee": _limb(lead, "hip", "knee") + _limb(lead, "knee", "ankle"),
        "lead-elbow": _limb(lead, "shoulder", "elbow") + _limb(lead, "elbow", "wrist"),
        "separation": [["left_shoulder", "right_shoulder"], ["left_hip", "right_hip"]],
        "pelvis-turn": [["left_hip", "right_hip"]],
        "pelvis-drift": [["left_hip", "right_hip"]],
        "trail-hand": _limb(trail, "elbow", "wrist"),
        "throwing-hand": _limb(trail, "elbow", "wrist"),
        "stride": _limb(lead, "knee", "ankle") + _limb(trail, "knee", "ankle"),
    }
    segments = by_series.get(series or "")
    return {"segments": segments} if segments else None
