#!/usr/bin/env python3
"""Cerebel-specific SOMA → OpenSim retargeting (replaces layup heuristics)."""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np

# Verified SOMA77 joint indices (see GEM-X postprocess SOMA77_CONTACT_JOINT_IDS).
SOMA_JOINT = {
    "hips": 0,
    "head": 6,
    # Glenohumeral / elbow / wrist — not clavicle (11/39 sit on the sternum).
    "l_shoulder": 12,
    "l_elbow": 13,
    "l_wrist": 14,
    "l_thumb": 17,
    "l_index": 22,
    "l_pinky": 37,
    "r_shoulder": 40,
    "r_elbow": 41,
    "r_wrist": 42,
    "r_thumb": 45,
    "r_index": 50,
    "r_pinky": 65,
    "l_hip": 67,
    "l_knee": 68,
    "l_ankle": 69,
    "l_toe": 70,
    "r_hip": 72,
    "r_knee": 73,
    "r_ankle": 74,
    "r_toe": 75,
}

# MediaPipe landmark indices used by run_opensim_ik.py
MP = {
    "l_shoulder": 11,
    "r_shoulder": 12,
    "l_elbow": 13,
    "r_elbow": 14,
    "l_wrist": 15,
    "r_wrist": 16,
    "l_pinky": 17,
    "r_pinky": 18,
    "l_index": 19,
    "r_index": 20,
    "l_thumb": 21,
    "r_thumb": 22,
    "l_hip": 23,
    "r_hip": 24,
    "l_knee": 25,
    "r_knee": 26,
    "l_ankle": 27,
    "r_ankle": 28,
    "l_toe": 31,
    "r_toe": 32,
}

SOMA_TO_MP: dict[int, int] = {
    SOMA_JOINT["head"]: 0,
    SOMA_JOINT["l_shoulder"]: MP["l_shoulder"],
    SOMA_JOINT["r_shoulder"]: MP["r_shoulder"],
    SOMA_JOINT["l_elbow"]: MP["l_elbow"],
    SOMA_JOINT["r_elbow"]: MP["r_elbow"],
    SOMA_JOINT["l_wrist"]: MP["l_wrist"],
    SOMA_JOINT["r_wrist"]: MP["r_wrist"],
    SOMA_JOINT["l_thumb"]: MP["l_thumb"],
    SOMA_JOINT["l_index"]: MP["l_index"],
    SOMA_JOINT["l_pinky"]: MP["l_pinky"],
    SOMA_JOINT["r_thumb"]: MP["r_thumb"],
    SOMA_JOINT["r_index"]: MP["r_index"],
    SOMA_JOINT["r_pinky"]: MP["r_pinky"],
    SOMA_JOINT["l_hip"]: MP["l_hip"],
    SOMA_JOINT["r_hip"]: MP["r_hip"],
    SOMA_JOINT["l_knee"]: MP["l_knee"],
    SOMA_JOINT["r_knee"]: MP["r_knee"],
    SOMA_JOINT["l_ankle"]: MP["l_ankle"],
    SOMA_JOINT["r_ankle"]: MP["r_ankle"],
    SOMA_JOINT["l_toe"]: MP["l_toe"],
    SOMA_JOINT["r_toe"]: MP["r_toe"],
}

MARKER_FROM_MP = {
    "L_SHOULDER": MP["l_shoulder"],
    "R_SHOULDER": MP["r_shoulder"],
    "L_ELBOW": MP["l_elbow"],
    "R_ELBOW": MP["r_elbow"],
    "L_WRIST": MP["l_wrist"],
    "R_WRIST": MP["r_wrist"],
    "L_HIP": MP["l_hip"],
    "R_HIP": MP["r_hip"],
    "L_KNEE": MP["l_knee"],
    "R_KNEE": MP["r_knee"],
    "L_ANKLE": MP["l_ankle"],
    "R_ANKLE": MP["r_ankle"],
    "L_TOE": MP["l_toe"],
    "R_TOE": MP["r_toe"],
}


def _facing_basis(ref: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Right / forward unit vectors in the ground XZ plane for one pose."""
    right = ref[SOMA_JOINT["r_hip"]] - ref[SOMA_JOINT["l_hip"]]
    right[1] = 0.0
    if np.linalg.norm(right) < 1e-6:
        right = ref[SOMA_JOINT["r_shoulder"]] - ref[SOMA_JOINT["l_shoulder"]]
        right[1] = 0.0
    right /= max(float(np.linalg.norm(right)), 1e-6)

    up = np.array([0.0, 1.0, 0.0])
    # Match OpenSim / GEM-X viser convention: forward = cross(up, right).
    forward = np.cross(up, right)
    fn = float(np.linalg.norm(forward))
    forward = np.array([0.0, 0.0, 1.0]) if fn < 1e-6 else forward / fn
    return right, forward


def normalize_soma77_joints(joints: np.ndarray, *, frame0: np.ndarray | None = None) -> np.ndarray:
    """Ground-align and yaw-normalize SOMA77 joints for OpenSim.

    Uses a **per-frame** hip/shoulder facing basis so full 360° turns do not
    accumulate yaw against a fixed frame-0 reference (which drives
    ``pelvis_rotation`` into its ±90° IK limits and tangles the skeleton).
    """
    j = np.array(joints, dtype=np.float64, copy=True)
    j[:, :, 1] -= j[:, :, 1].min()

    if frame0 is not None:
        right, forward = _facing_basis(frame0)
        return np.stack([j @ right, j[..., 1], j @ forward], axis=-1)

    aligned = np.empty_like(j)
    for t in range(j.shape[0]):
        right, forward = _facing_basis(j[t])
        aligned[t, :, 0] = j[t] @ right
        aligned[t, :, 1] = j[t, :, 1]
        aligned[t, :, 2] = j[t] @ forward
    return aligned


def global_yaw_series(joints: np.ndarray) -> np.ndarray:
    """Continuous root yaw (rad) from SOMA77 hips in the ground-aligned world frame."""
    j = np.array(joints, dtype=np.float64, copy=True)
    j[:, :, 1] -= j[:, :, 1].min()
    yaws = np.empty(j.shape[0], dtype=np.float64)
    for t in range(j.shape[0]):
        _, forward = _facing_basis(j[t])
        yaws[t] = math.atan2(forward[0], forward[2])
    return np.unwrap(yaws)


def _rotation_y(angle: float) -> np.ndarray:
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]], dtype=np.float64)


def _quat_from_y(angle: float) -> np.ndarray:
    half = angle * 0.5
    return np.array([0.0, math.sin(half), 0.0, math.cos(half)], dtype=np.float64)


def _quat_conj(q: np.ndarray) -> np.ndarray:
    return np.array([-q[0], -q[1], -q[2], q[3]], dtype=np.float64)


def _quat_mul(q_left: np.ndarray, q_right: np.ndarray) -> np.ndarray:
    """Hamilton product for xyzw quaternions."""
    x1, y1, z1, w1 = q_left
    x2, y2, z2, w2 = q_right
    return np.array(
        [
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        ],
        dtype=np.float64,
    )


def apply_global_yaw_to_motion(frames: list[dict], yaw_rad: np.ndarray) -> None:
    """Re-apply root yaw removed by per-frame IK canonicalization (viewer only)."""
    if not frames:
        return
    yaw_rel = np.asarray(yaw_rad, dtype=np.float64)
    if yaw_rel.shape[0] != len(frames):
        return
    yaw_rel = yaw_rel - yaw_rel[0]

    for t, frame in enumerate(frames):
        angle = float(yaw_rel[t])
        rot = _rotation_y(angle)
        q_yaw = _quat_from_y(angle)

        for body in frame.get("bodies", {}).values():
            p = rot @ np.asarray(body["p"], dtype=np.float64)
            q = _quat_mul(q_yaw, np.asarray(body["q"], dtype=np.float64))
            q /= max(float(np.linalg.norm(q)), 1e-8)
            body["p"] = [round(float(v), 6) for v in p]
            body["q"] = [round(float(v), 8) for v in q]

        if "pelvis" in frame:
            p = rot @ np.asarray(frame["pelvis"], dtype=np.float64)
            frame["pelvis"] = [round(float(v), 6) for v in p]
        if "centerOfMass" in frame:
            p = rot @ np.asarray(frame["centerOfMass"], dtype=np.float64)
            frame["centerOfMass"] = [round(float(v), 6) for v in p]
        for muscle in frame.get("muscles", []):
            muscle["points"] = [
                [round(float(v), 6) for v in rot @ np.asarray(pt, dtype=np.float64)]
                for pt in muscle["points"]
            ]
        coords = frame.setdefault("coordinates", {})
        if "pelvis_rotation" in coords:
            coords["pelvis_rotation"] = round(float(coords["pelvis_rotation"] + angle), 8)


def make_body_quaternions_continuous(frames: list[dict], body_names: Iterable[str]) -> None:
    """Flip q/-q on successive frames so Three.js slerp does not spin bodies."""
    prev: dict[str, np.ndarray] = {}
    for frame in frames:
        bodies = frame.get("bodies", {})
        for name in body_names:
            body = bodies.get(name)
            if not body or "q" not in body:
                continue
            q = np.asarray(body["q"], dtype=np.float64)
            pn = float(np.linalg.norm(q))
            if pn > 1e-8:
                q /= pn
            if name in prev and float(np.dot(q, prev[name])) < 0.0:
                q = -q
            body["q"] = [round(float(v), 8) for v in q]
            prev[name] = q


def unwrap_coordinates(frames: list[dict], names: Iterable[str]) -> None:
    """Remove 2π jumps from rotational coordinates (display / playback stability)."""
    if not frames:
        return
    series = {name: np.array([f["coordinates"].get(name, 0.0) for f in frames]) for name in names}
    unwrapped = {name: np.unwrap(values) for name, values in series.items()}
    for t, frame in enumerate(frames):
        coords = frame.setdefault("coordinates", {})
        for name, values in unwrapped.items():
            coords[name] = round(float(values[t]), 8)


def soma_yup_to_opensim(points: np.ndarray) -> np.ndarray:
    """Map SOMA77-normalized joints to OpenSim ground (+Y up, +X forward, +Z right).

    After ``normalize_soma77_joints`` the SOMA frame is:
      +X right, +Y up, +Z forward (facing).

    OpenSim uses +X forward, +Y up, +Z right.
    """
    out = np.empty_like(points)
    out[:, 0] = points[:, 2]   # forward → OpenSim +X
    out[:, 1] = points[:, 1]   # up      → OpenSim +Y
    out[:, 2] = points[:, 0]   # right   → OpenSim +Z
    return out


def project_to_image(
    points_cam: np.ndarray,
    K: np.ndarray,
    image_size: tuple[int, int],
) -> np.ndarray:
    """Project camera-space points to normalized image coordinates [0,1]."""
    width, height = image_size
    z = np.clip(points_cam[:, 2], 1e-4, None)
    u = K[0, 0] * points_cam[:, 0] / z + K[0, 2]
    v = K[1, 1] * points_cam[:, 1] / z + K[1, 2]
    return np.stack([u / width, v / height, points_cam[:, 2] / z], axis=-1)


def build_mediapipe_landmarks(
    joints_opensim: np.ndarray,
    joints_cam: np.ndarray | None,
    K: np.ndarray | None,
    image_size: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    """Build 33-landmark arrays in OpenSim-aligned world and normalized image coords."""
    world = np.zeros((33, 3), dtype=np.float64)
    for soma_idx, mp_idx in SOMA_TO_MP.items():
        world[mp_idx] = joints_opensim[soma_idx]

    # Fill face / hand / heel placeholders from nearest mapped joints.
    world[0] = joints_opensim[SOMA_JOINT["head"]]
    for idx in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
        world[idx] = world[0]
    world[29] = world[MP["l_ankle"]]
    world[30] = world[MP["r_ankle"]]

    if joints_cam is not None and K is not None:
        cam_world = np.zeros_like(world)
        for soma_idx, mp_idx in SOMA_TO_MP.items():
            cam_world[mp_idx] = joints_cam[soma_idx]
        cam_world[0] = joints_cam[SOMA_JOINT["head"]]
        image = project_to_image(cam_world, K, image_size)
    else:
        image = np.zeros_like(world)
        image[:, 0] = 0.5
        image[:, 1] = 0.5

    return world, image


def direct_segment_targets(pose3d: dict) -> tuple[list[float], list[dict[str, np.ndarray]]]:
    """Place OpenSim virtual markers at actual retargeted SOMA landmark positions."""
    times: list[float] = []
    targets: list[dict[str, np.ndarray]] = []

    for frame in pose3d["frames"]:
        world = np.array(frame["world"], dtype=np.float64)
        times.append(float(frame["time"]))
        frame_targets = {
            marker: world[mp_idx].copy()
            for marker, mp_idx in MARKER_FROM_MP.items()
        }
        targets.append(frame_targets)

    return times, targets


def _unit(vec: np.ndarray, fallback: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(vec))
    return vec / n if n > 1e-8 else fallback


def _rotation_matrix_to_quat(rot: np.ndarray) -> np.ndarray:
    """Convert a 3×3 rotation matrix to an xyzw quaternion."""
    m = np.asarray(rot, dtype=np.float64)
    trace = float(m[0, 0] + m[1, 1] + m[2, 2])
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[0, 1] + m[1, 0]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[0, 1] + m[1, 0]) / s
        y = 0.25 * s
        z = (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[1, 2] + m[2, 1]) / s
        z = 0.25 * s
    q = np.array([x, y, z, w], dtype=np.float64)
    q /= max(float(np.linalg.norm(q)), 1e-8)
    return q


def _palm_quaternion(
    wrist: np.ndarray,
    thumb: np.ndarray,
    index: np.ndarray,
    pinky: np.ndarray,
    *,
    is_left: bool,
) -> np.ndarray | None:
    """Ground-frame palm orientation from SOMA fingertip landmarks."""
    finger = _unit(index - wrist, np.array([1.0, 0.0, 0.0]))
    across_raw = _unit(pinky - thumb, np.array([0.0, 0.0, 1.0]))
    palm = _unit(np.cross(finger, across_raw), np.array([0.0, 1.0, 0.0]))
    if is_left:
        palm = -palm
    across = _unit(np.cross(finger, palm), np.array([0.0, 0.0, 1.0]))
    rot = np.column_stack([finger, palm, across])
    if abs(float(np.linalg.det(rot)) - 1.0) > 0.05:
        return None
    return _rotation_matrix_to_quat(rot)


def _hand_side_specs() -> tuple[tuple[str, bool, int, int, int, int], ...]:
    return (
        ("hand_l", True, MP["l_wrist"], MP["l_thumb"], MP["l_index"], MP["l_pinky"]),
        ("hand_r", False, MP["r_wrist"], MP["r_thumb"], MP["r_index"], MP["r_pinky"]),
    )


def apply_hand_retargeting_to_motion(frames: list[dict], pose3d: dict) -> None:
    """Drive OpenSim hand body orientations from SOMA fingertip landmarks.

    Wrist flex/dev coordinates are locked in the Rajagopal model, so IK leaves
    ``hand_*`` aligned with ``radius_*``.  This post-pass applies the change in
    SOMA palm orientation relative to the solved forearm, preserving IK wrist
    position and arm pose.
    """
    if not frames or not pose3d.get("frames"):
        return
    if len(frames) != len(pose3d["frames"]):
        return

    for body_name, is_left, wrist_i, thumb_i, index_i, pinky_i in _hand_side_specs():
        radius_name = "radius_l" if is_left else "radius_r"
        if body_name not in frames[0].get("bodies", {}) or radius_name not in frames[0]["bodies"]:
            continue

        w0 = np.asarray(pose3d["frames"][0]["world"], dtype=np.float64)
        q_soma0 = _palm_quaternion(
            w0[wrist_i], w0[thumb_i], w0[index_i], w0[pinky_i], is_left=is_left
        )
        if q_soma0 is None:
            continue
        q_rad0 = np.asarray(frames[0]["bodies"][radius_name]["q"], dtype=np.float64)
        q_rad0 /= max(float(np.linalg.norm(q_rad0)), 1e-8)
        rel0 = _quat_mul(_quat_conj(q_rad0), q_soma0)

        for t, frame in enumerate(frames):
            world = np.asarray(pose3d["frames"][t]["world"], dtype=np.float64)
            bodies = frame.get("bodies", {})
            if body_name not in bodies or radius_name not in bodies:
                continue
            q_soma = _palm_quaternion(
                world[wrist_i],
                world[thumb_i],
                world[index_i],
                world[pinky_i],
                is_left=is_left,
            )
            if q_soma is None:
                continue
            q_rad = np.asarray(bodies[radius_name]["q"], dtype=np.float64)
            q_rad /= max(float(np.linalg.norm(q_rad)), 1e-8)
            rel_t = _quat_mul(_quat_conj(q_rad), q_soma)
            delta = _quat_mul(rel_t, _quat_conj(rel0))
            q_hand = _quat_mul(delta, q_rad)
            q_hand /= max(float(np.linalg.norm(q_hand)), 1e-8)
            bodies[body_name]["q"] = [round(float(v), 8) for v in q_hand]


def alignment_error_report(
    soma_joints_opensim: np.ndarray,
    opensim_frames: list[dict],
    *,
    sample_indices: Iterable[int] | None = None,
) -> dict:
    """Compare SOMA ground-aligned joints to solved OpenSim body origins."""
    body_map = {
        "l_hip": "femur_l",
        "l_knee": "tibia_l",
        "l_ankle": "talus_l",
        "r_hip": "femur_r",
        "r_knee": "tibia_r",
        "r_ankle": "talus_r",
        "l_shoulder": "humerus_l",
        "l_elbow": "ulna_l",
        "r_shoulder": "humerus_r",
        "r_elbow": "ulna_r",
    }
    if sample_indices is None:
        sample_indices = [0, len(opensim_frames) // 2, len(opensim_frames) - 1]

    per_joint: dict[str, list[float]] = {k: [] for k in body_map}
    for fi in sample_indices:
        if fi >= len(opensim_frames):
            continue
        bodies = opensim_frames[fi]["bodies"]
        for soma_key, body_name in body_map.items():
            soma_idx = SOMA_JOINT[soma_key]
            if body_name not in bodies:
                continue
            osim_p = np.array(bodies[body_name]["p"], dtype=np.float64)
            err = float(np.linalg.norm(soma_joints_opensim[fi, soma_idx] - osim_p))
            per_joint[soma_key].append(err)

    summary = {}
    all_errs = []
    for key, errs in per_joint.items():
        if errs:
            summary[key] = {
                "mean_m": round(float(np.mean(errs)), 4),
                "max_m": round(float(np.max(errs)), 4),
            }
            all_errs.extend(errs)
    summary["overall_mean_m"] = round(float(np.mean(all_errs)), 4) if all_errs else None
    summary["overall_max_m"] = round(float(np.max(all_errs)), 4) if all_errs else None
    return summary
