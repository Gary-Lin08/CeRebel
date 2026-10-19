"""Deterministic motion event detection from kinematics + contact."""

from __future__ import annotations

from typing import Any

import numpy as np

from motion_agent.motion.sequence import MotionSequence, velocity_of
from motion_agent.motion.synchronization import slice_indices
from motion_agent.storage.errors import tool_error


def _local_extrema(series: np.ndarray, timestamps: np.ndarray, order: int = 2) -> list[dict]:
    events: list[dict] = []
    if series.size < 2 * order + 1:
        return events
    for i in range(order, series.size - order):
        window = series[i - order : i + order + 1]
        if series[i] == np.max(window) and series[i] > series[i - 1] and series[i] >= series[i + 1]:
            events.append({"kind": "local_max", "index": i, "timestamp": float(timestamps[i]), "value": float(series[i])})
        if series[i] == np.min(window) and series[i] < series[i - 1] and series[i] <= series[i + 1]:
            events.append({"kind": "local_min", "index": i, "timestamp": float(timestamps[i]), "value": float(series[i])})
    return events


def get_motion_events(
    seq: MotionSequence,
    start_time: float,
    end_time: float,
    max_events: int = 40,
) -> dict[str, Any]:
    try:
        t0, t1 = float(start_time), float(end_time)
    except (TypeError, ValueError):
        return tool_error("INVALID_ARGUMENT", "start_time/end_time must be floats")
    if t1 < t0:
        t0, t1 = t1, t0
    sl = slice_indices(seq.timestamps, t0, t1)
    if sl.stop - sl.start < 3:
        return tool_error(
            "INSUFFICIENT_WINDOW",
            "Need a wider temporal window to detect events",
            start_time=t0,
            end_time=t1,
        )

    ts = seq.timestamps[sl]
    dt = float(seq.metadata.get("dt") or (1.0 / seq.fps))
    events: list[dict[str, Any]] = []

    # Knee flexion extrema
    for joint, label in (("right_knee", "right"), ("left_knee", "left")):
        if joint not in seq.joint_angles_rad:
            continue
        ang_deg = np.degrees(seq.joint_angles_rad[joint][sl])
        for ex in _local_extrema(ang_deg, ts, order=max(2, int(0.05 / dt))):
            events.append(
                {
                    "event_type": "maximum_knee_flexion"
                    if ex["kind"] == "local_max"
                    else "minimum_knee_flexion",
                    "timestamp": round(ex["timestamp"], 5),
                    "involved_joints": [joint],
                    "supporting_measurements": {
                        "flexion_deg": round(ex["value"], 3),
                        "side": label,
                    },
                    "confidence": 0.7,
                }
            )
        # extension onset: angular velocity crosses from +flex toward extension
        # OpenSim knee_angle is typically flexion; negative velocity ≈ extending
        ang_vel = np.gradient(ang_deg, dt)
        for i in range(1, len(ang_vel)):
            if ang_vel[i - 1] > 5 and ang_vel[i] <= 5:
                events.append(
                    {
                        "event_type": "knee_extension_onset",
                        "timestamp": round(float(ts[i]), 5),
                        "involved_joints": [joint],
                        "supporting_measurements": {
                            "angular_velocity_deg_s": round(float(ang_vel[i]), 3),
                        },
                        "confidence": 0.55,
                    }
                )

    # Contact transitions
    left = seq.contact_left[sl]
    right = seq.contact_right[sl]
    for i in range(1, len(ts)):
        if left[i] and not left[i - 1]:
            events.append(
                {
                    "event_type": "foot_contact",
                    "timestamp": round(float(ts[i]), 5),
                    "involved_joints": ["left_foot"],
                    "supporting_measurements": {"side": "left"},
                    "confidence": 0.6,
                }
            )
        if (not left[i]) and left[i - 1]:
            events.append(
                {
                    "event_type": "foot_contact_release",
                    "timestamp": round(float(ts[i]), 5),
                    "involved_joints": ["left_foot"],
                    "supporting_measurements": {"side": "left"},
                    "confidence": 0.6,
                }
            )
        if right[i] and not right[i - 1]:
            events.append(
                {
                    "event_type": "foot_contact",
                    "timestamp": round(float(ts[i]), 5),
                    "involved_joints": ["right_foot"],
                    "supporting_measurements": {"side": "right"},
                    "confidence": 0.6,
                }
            )
        if (not right[i]) and right[i - 1]:
            events.append(
                {
                    "event_type": "foot_contact_release",
                    "timestamp": round(float(ts[i]), 5),
                    "involved_joints": ["right_foot"],
                    "supporting_measurements": {"side": "right"},
                    "confidence": 0.6,
                }
            )

    # COM vertical extrema / jump candidates
    com_y = seq.com_position[sl, 1]
    com_vy = velocity_of(seq.com_position, dt)[sl, 1]
    for ex in _local_extrema(com_y, ts, order=max(2, int(0.08 / dt))):
        events.append(
            {
                "event_type": "com_height_peak" if ex["kind"] == "local_max" else "com_height_trough",
                "timestamp": round(ex["timestamp"], 5),
                "involved_joints": ["pelvis"],
                "supporting_measurements": {"com_height_m": round(ex["value"], 5)},
                "confidence": 0.75,
            }
        )

    # Jump / takeoff / landing heuristics (flight + COM vertical evidence)
    both_off = (~left) & (~right)
    if np.any(both_off):
        padded = np.concatenate([[False], both_off, [False]])
        d = np.diff(padded.astype(int))
        starts = np.where(d == 1)[0]
        ends = np.where(d == -1)[0]
        for s, e in zip(starts, ends):
            last = min(e - 1, len(ts) - 1)
            dur = float(ts[last] - ts[s])
            if dur < 0.05:
                continue
            peak_vy = float(np.max(com_vy[s : last + 1]))
            peak_h = float(np.max(com_y[s : last + 1]))
            takeoff_vy = float(com_vy[s])
            conf = 0.45
            if dur >= 0.12 and peak_vy > 0.4:
                conf = 0.82
            elif dur >= 0.08 and takeoff_vy > 0.15:
                conf = 0.65
            events.append(
                {
                    "event_type": "jump_takeoff_candidate",
                    "timestamp": round(float(ts[s]), 5),
                    "involved_joints": ["left_foot", "right_foot"],
                    "supporting_measurements": {
                        "flight_duration_s": round(dur, 4),
                        "com_vertical_velocity_m_s": round(takeoff_vy, 4),
                        "peak_com_vertical_velocity_m_s": round(peak_vy, 4),
                        "peak_com_height_m": round(peak_h, 4),
                    },
                    "confidence": conf,
                }
            )
            events.append(
                {
                    "event_type": "landing_candidate",
                    "timestamp": round(float(ts[last]), 5),
                    "involved_joints": ["left_foot", "right_foot"],
                    "supporting_measurements": {
                        "flight_duration_s": round(dur, 4),
                        "com_vertical_velocity_m_s": round(float(com_vy[last]), 4),
                    },
                    "confidence": conf,
                }
            )

    # Acceleration peaks on COM
    com_ay = np.gradient(com_vy, dt)
    peak_i = int(np.argmax(np.abs(com_ay)))
    events.append(
        {
            "event_type": "acceleration_peak",
            "timestamp": round(float(ts[peak_i]), 5),
            "involved_joints": ["pelvis"],
            "supporting_measurements": {
                "com_vertical_acceleration_m_s2": round(float(com_ay[peak_i]), 4)
            },
            "confidence": 0.7,
        }
    )

    # Velocity reversal of COM vertical
    for i in range(1, len(com_vy)):
        if com_vy[i - 1] > 0.05 and com_vy[i] <= 0.0:
            events.append(
                {
                    "event_type": "velocity_reversal",
                    "timestamp": round(float(ts[i]), 5),
                    "involved_joints": ["pelvis"],
                    "supporting_measurements": {
                        "axis": "com_vertical",
                        "from_m_s": round(float(com_vy[i - 1]), 4),
                        "to_m_s": round(float(com_vy[i]), 4),
                    },
                    "confidence": 0.7,
                }
            )

    events.sort(key=lambda e: (e["timestamp"], e["event_type"]))
    # Deduplicate near-identical events
    deduped: list[dict[str, Any]] = []
    for ev in events:
        if deduped and deduped[-1]["event_type"] == ev["event_type"] and abs(
            deduped[-1]["timestamp"] - ev["timestamp"]
        ) < 0.04:
            continue
        deduped.append(ev)

    return {
        "error": False,
        "start_time": float(ts[0]),
        "end_time": float(ts[-1]),
        "event_count": min(len(deduped), max_events),
        "events": deduped[:max_events],
        "note": "Events are deterministic signal heuristics, not learned classifiers.",
    }
