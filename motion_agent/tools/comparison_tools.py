"""Motion comparison and search stubs/heuristics."""

from __future__ import annotations

from typing import Any

import numpy as np

from motion_agent.dynamics.joint_dynamics import get_joint_power
from motion_agent.motion.kinematics import get_com_trajectory, get_contact_state, get_joint_trajectory
from motion_agent.motion.sequence import MotionSequence
from motion_agent.storage.errors import tool_error


def compare_motion(
    seq: MotionSequence,
    segment_a: dict[str, float],
    segment_b: dict[str, float],
    joints: list[str] | None = None,
) -> dict[str, Any]:
    """Compare two temporal segments of the same sequence."""
    try:
        a0, a1 = float(segment_a["start_time"]), float(segment_a["end_time"])
        b0, b1 = float(segment_b["start_time"]), float(segment_b["end_time"])
    except (KeyError, TypeError, ValueError):
        return tool_error(
            "INVALID_ARGUMENT",
            "segment_a/segment_b need start_time and end_time floats",
        )

    joints = joints or ["right_knee", "left_knee", "right_hip", "left_hip"]
    semantic: list[str] = []
    numerical: dict[str, Any] = {
        "duration_a_s": round(a1 - a0, 4),
        "duration_b_s": round(b1 - b0, 4),
        "duration_delta_s": round((b1 - b0) - (a1 - a0), 4),
        "joints": {},
    }

    for joint in joints:
        ta = get_joint_trajectory(seq, joint, a0, a1, quantities=["angle", "angular_velocity"])
        tb = get_joint_trajectory(seq, joint, b0, b1, quantities=["angle", "angular_velocity"])
        if ta.get("error") or tb.get("error"):
            continue
        max_a = ta["statistics"].get("max_angle_deg")
        max_b = tb["statistics"].get("max_angle_deg")
        if max_a is None or max_b is None:
            continue
        delta = max_b - max_a
        numerical["joints"][joint] = {
            "max_angle_deg_a": max_a,
            "max_angle_deg_b": max_b,
            "max_angle_delta_deg": round(delta, 3),
            "peak_ang_vel_a": ta["statistics"].get("peak_angular_velocity_deg_s"),
            "peak_ang_vel_b": tb["statistics"].get("peak_angular_velocity_deg_s"),
        }
        semantic.append(
            f"Segment B has approximately {abs(delta):.1f}° "
            f"{'greater' if delta > 0 else 'less'} peak {joint} flexion."
        )

    com_a = get_com_trajectory(seq, a0, a1)
    com_b = get_com_trajectory(seq, b0, b1)
    if not com_a.get("error") and not com_b.get("error"):
        ha = com_a["statistics"]["max_com_height_m"]
        hb = com_b["statistics"]["max_com_height_m"]
        numerical["com_max_height_delta_m"] = round(hb - ha, 4)
        semantic.append(
            f"Peak COM height differs by approximately {abs(hb - ha):.3f} m "
            f"(B {'higher' if hb > ha else 'lower'})."
        )

    ca = get_contact_state(seq, a0, a1)
    cb = get_contact_state(seq, b0, b1)
    if not ca.get("error") and not cb.get("error"):
        numerical["contact"] = {
            "left_fraction_a": ca["left_foot_contact_fraction"],
            "left_fraction_b": cb["left_foot_contact_fraction"],
            "right_fraction_a": ca["right_foot_contact_fraction"],
            "right_fraction_b": cb["right_foot_contact_fraction"],
        }

    if seq.has_dynamics():
        pa = get_joint_power(seq, a0, a1)
        pb = get_joint_power(seq, b0, b1)
        if not pa.get("error") and not pb.get("error"):
            for joint in ("right_hip", "right_knee", "left_hip", "left_knee"):
                ja = (pa.get("joints") or {}).get(joint) or {}
                jb = (pb.get("joints") or {}).get(joint) or {}
                if not ja or not jb or ja.get("error") or jb.get("error"):
                    continue
                a_pow = ja.get("peak_positive_power_w") or 0.0
                b_pow = jb.get("peak_positive_power_w") or 0.0
                if abs(a_pow) < 1e-6:
                    continue
                pct = 100.0 * (b_pow - a_pow) / abs(a_pow)
                semantic.append(
                    f"Peak {joint} positive power is approximately {pct:.0f}% "
                    f"{'higher' if pct > 0 else 'lower'} in segment B."
                )

    return {
        "error": False,
        "segment_a": {"start_time": a0, "end_time": a1},
        "segment_b": {"start_time": b0, "end_time": b1},
        "numerical_differences": numerical,
        "semantic_differences": semantic,
    }


def search_motion(seq: MotionSequence, description: str) -> dict[str, Any]:
    """Deterministic heuristic search — not semantic RAG.

    Supported patterns (case-insensitive substrings):
    - jump / flight
    - squat / deep knee
    - right hand above head / hand overhead
    """
    q = (description or "").strip().lower()
    if not q:
        return tool_error("INVALID_ARGUMENT", "description must be non-empty")

    hits: list[dict[str, Any]] = []
    dt = float(seq.metadata.get("dt") or (1.0 / seq.fps))

    if any(k in q for k in ("jump", "flight", "airborne")):
        both_off = (~seq.contact_left) & (~seq.contact_right)
        padded = np.concatenate([[False], both_off, [False]])
        d = np.diff(padded.astype(int))
        starts = np.where(d == 1)[0]
        ends = np.where(d == -1)[0]
        for s, e in zip(starts, ends):
            dur = float(seq.timestamps[min(e - 1, len(seq.timestamps) - 1)] - seq.timestamps[s])
            if dur >= 0.08:
                hits.append(
                    {
                        "match_type": "jump_candidate",
                        "start_time": round(float(seq.timestamps[s]), 5),
                        "end_time": round(
                            float(seq.timestamps[min(e - 1, len(seq.timestamps) - 1)]), 5
                        ),
                        "score": min(1.0, dur / 0.4),
                        "evidence": {"flight_duration_s": round(dur, 4)},
                    }
                )

    if any(k in q for k in ("squat", "deep knee", "crouch")):
        for joint in ("right_knee", "left_knee"):
            if joint not in seq.joint_angles_rad:
                continue
            ang = np.degrees(seq.joint_angles_rad[joint])
            # deep flexion threshold ~ 90 deg (OpenSim knee angle)
            deep = ang > 90.0
            padded = np.concatenate([[False], deep, [False]])
            d = np.diff(padded.astype(int))
            starts = np.where(d == 1)[0]
            ends = np.where(d == -1)[0]
            for s, e in zip(starts, ends):
                mid = (s + e) // 2
                hits.append(
                    {
                        "match_type": "deep_squat_candidate",
                        "start_time": round(float(seq.timestamps[s]), 5),
                        "end_time": round(
                            float(seq.timestamps[min(e - 1, len(seq.timestamps) - 1)]), 5
                        ),
                        "score": round(float(min(1.0, ang[mid] / 120.0)), 3),
                        "evidence": {
                            "joint": joint,
                            "peak_flexion_deg": round(float(np.max(ang[s:e])), 2),
                        },
                    }
                )

    if any(k in q for k in ("hand above head", "hand overhead", "right hand above", "arm raised")):
        head = seq.joint_positions.get("head")
        # head may be missing; use COM + wrist relative to shoulder
        wrist_key = "right_wrist" if "right" in q or "right hand" in q else "right_wrist"
        if "left" in q:
            wrist_key = "left_wrist"
        wrist = seq.joint_positions.get(wrist_key)
        shoulder = seq.joint_positions.get(
            "right_shoulder" if "right" in wrist_key else "left_shoulder"
        )
        if wrist is not None and shoulder is not None:
            # hand above head approx: wrist y > shoulder y + 0.25 m, or above COM + 0.35
            ref_y = seq.com_position[:, 1] + 0.35
            if head is not None:
                ref_y = head[:, 1]
            above = wrist[:, 1] > ref_y
            padded = np.concatenate([[False], above, [False]])
            d = np.diff(padded.astype(int))
            starts = np.where(d == 1)[0]
            ends = np.where(d == -1)[0]
            for s, e in zip(starts, ends):
                if e - s < max(2, int(0.1 / dt)):
                    continue
                hits.append(
                    {
                        "match_type": "hand_above_head_candidate",
                        "start_time": round(float(seq.timestamps[s]), 5),
                        "end_time": round(
                            float(seq.timestamps[min(e - 1, len(seq.timestamps) - 1)]), 5
                        ),
                        "score": 0.7,
                        "evidence": {"joint": wrist_key},
                    }
                )

    if not hits:
        return {
            "error": False,
            "query": description,
            "matches": [],
            "note": (
                "No heuristic matches. Supported demos: jumps, deep squats, "
                "hand above head. Full semantic search is not implemented yet."
            ),
            "implemented": True,
        }

    hits.sort(key=lambda h: h.get("score", 0), reverse=True)
    return {
        "error": False,
        "query": description,
        "matches": hits[:20],
        "note": "Deterministic heuristics only — not embedding/RAG search.",
        "implemented": True,
    }
