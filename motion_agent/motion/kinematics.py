"""Deterministic kinematic helpers on MotionSequence."""

from __future__ import annotations

from typing import Any

import numpy as np

from motion_agent.motion.angles import (
    SOMERSAULT_CONVENTION,
    somersault_direction_label,
    wrap_risk_metadata,
)
from motion_agent.motion.sequence import MotionSequence, acceleration_of, velocity_of
from motion_agent.motion.smplx_mapping import resolve_joint_name
from motion_agent.motion.synchronization import (
    clamp_timestamp,
    nearest_index,
    slice_indices,
    timestamp_to_frame,
)
from motion_agent.storage.errors import tool_error


def _rad_to_deg(x: float | np.ndarray) -> float | np.ndarray:
    return np.degrees(x)


def get_motion_state(
    seq: MotionSequence,
    timestamp: float,
    temporal_window: float | None = None,
    joints: list[str] | None = None,
) -> dict[str, Any]:
    """Instantaneous reconstructed state around a timestamp."""
    try:
        t = float(timestamp)
    except (TypeError, ValueError):
        return tool_error("INVALID_ARGUMENT", "timestamp must be a float (seconds)")

    if t < -1e-6 or t > seq.duration_s + 1e-3:
        return tool_error(
            "TIMESTAMP_OUT_OF_RANGE",
            f"timestamp {t} outside [0, {seq.duration_s:.4f}]",
            duration_s=seq.duration_s,
        )

    t = clamp_timestamp(t, seq.duration_s)
    idx = nearest_index(seq.timestamps, t)
    actual_t = float(seq.timestamps[idx])
    video_frame = timestamp_to_frame(t, seq.fps, seq.frame_count)

    joint_names = joints or [
        "pelvis",
        "left_hip",
        "right_hip",
        "left_knee",
        "right_knee",
        "left_ankle",
        "right_ankle",
        "left_shoulder",
        "right_shoulder",
        "left_elbow",
        "right_elbow",
        "left_wrist",
        "right_wrist",
        "spine3",
        "head",
    ]

    dt = float(seq.metadata.get("dt") or (1.0 / seq.fps))
    joint_out: dict[str, Any] = {}
    for raw in joint_names:
        try:
            name = resolve_joint_name(raw)
        except KeyError:
            joint_out[raw] = tool_error("UNSUPPORTED_JOINT", f"Unknown joint {raw!r}")
            continue
        entry: dict[str, Any] = {"name": name}
        if name in seq.joint_angles_rad:
            ang = float(seq.joint_angles_rad[name][idx])
            entry["flexion_rad"] = round(ang, 6)
            entry["flexion_deg"] = round(float(_rad_to_deg(ang)), 3)
            # angular velocity from continuous (unwrapped) angle series
            ang_series = seq.joint_angles_rad[name]
            ang_vel = float(np.gradient(ang_series, dt)[idx])
            entry["angular_velocity_rad_s"] = round(ang_vel, 6)
            entry["angular_velocity_deg_s"] = round(float(_rad_to_deg(ang_vel)), 3)
            if name in seq.joint_angles_rad_raw:
                raw = float(seq.joint_angles_rad_raw[name][idx])
                entry["opensim_dof_raw_deg"] = round(float(_rad_to_deg(raw)), 3)
                euler_match = None
                if name == "pelvis" and seq.root_euler_xyz_rad.size:
                    # pelvis_tilt correlates best with continuous Euler X on Rajagopal
                    euler_match = float(seq.root_euler_xyz_rad[idx, 0])
                risk = wrap_risk_metadata(raw, euler_match)
                entry["wrap_risk"] = risk
                if risk.get("near_singularity_or_wrap"):
                    entry["angle_reliability"] = "low_possible_ik_wrap"
                    entry["preferred_orientation_source"] = (
                        "root_euler_continuous_deg / root_cumulative_rotation_deg "
                        "(extent); intrinsic_somersault_* (front vs back)"
                    )
        if name == "pelvis":
            eul = seq.root_euler_xyz_rad[idx]
            entry["root_euler_frame"] = "world"
            entry["root_euler_continuous_deg"] = {
                "x": round(float(_rad_to_deg(eul[0])), 3),
                "y": round(float(_rad_to_deg(eul[1])), 3),
                "z": round(float(_rad_to_deg(eul[2])), 3),
            }
            entry["root_cumulative_rotation_deg"] = round(
                float(_rad_to_deg(seq.root_cumulative_rotation_rad[idx])), 3
            )
            # Angular rates from continuous Euler (more reliable through flips)
            eul_vel = np.gradient(seq.root_euler_xyz_rad, dt, axis=0)[idx]
            entry["root_euler_rate_deg_s"] = {
                "x": round(float(_rad_to_deg(eul_vel[0])), 3),
                "y": round(float(_rad_to_deg(eul_vel[1])), 3),
                "z": round(float(_rad_to_deg(eul_vel[2])), 3),
            }
            entry["intrinsic_somersault_rate_deg_s"] = round(
                float(_rad_to_deg(seq.intrinsic_somersault_rate_rad_s[idx])), 3
            )
            entry["intrinsic_somersault_cumulative_deg"] = round(
                float(_rad_to_deg(seq.intrinsic_somersault_cumulative_rad[idx])), 3
            )
            entry["rider_facing_horizontal"] = [
                round(float(v), 4) for v in seq.rider_facing_horizontal[idx]
            ]
            entry["rider_lateral_horizontal"] = [
                round(float(v), 4) for v in seq.rider_lateral_horizontal[idx]
            ]
            entry["somersault_convention"] = SOMERSAULT_CONVENTION
            entry["orientation_note"] = (
                "root_euler_* is OpenSim WORLD (Y-up) — good for flip extent, not for "
                "front vs back. Prefer intrinsic_somersault_* (+ = backflip direction, "
                "− = frontflip). OpenSim pelvis_tilt can stall near ±180°."
            )
        if name in seq.joint_angular_velocity_rad_s:
            w = float(seq.joint_angular_velocity_rad_s[name][idx])
            entry["opensim_angular_velocity_rad_s"] = round(w, 6)
            entry["opensim_angular_velocity_deg_s"] = round(float(_rad_to_deg(w)), 3)
        if name in seq.joint_positions:
            pos = seq.joint_positions[name]
            entry["position_m"] = [round(float(v), 5) for v in pos[idx]]
            vel = velocity_of(pos, dt)[idx]
            acc = acceleration_of(pos, dt)[idx]
            entry["velocity_m_s"] = [round(float(v), 5) for v in vel]
            entry["acceleration_m_s2"] = [round(float(v), 5) for v in acc]
        if name in seq.confidence:
            entry["confidence"] = round(float(seq.confidence[name][idx]), 4)
        if len(entry) == 1:
            entry["available"] = False
            entry["note"] = "No OpenSim angle/position for this joint in current pipeline"
        joint_out[name] = entry

    com = seq.com_position[idx]
    com_vel = velocity_of(seq.com_position, dt)[idx]
    com_acc = acceleration_of(seq.com_position, dt)[idx]

    result: dict[str, Any] = {
        "error": False,
        "timestamp_requested": t,
        "timestamp_actual": actual_t,
        "motion_frame_index": idx,
        "video_frame_index": video_frame,
        "coordinate_system": seq.metadata.get("coordinate_system"),
        "units": seq.metadata.get("units"),
        "root": {
            "frame": "world",
            "position_m": [round(float(v), 5) for v in seq.root_position[idx]],
            "rotation_quat_xyzw": [round(float(v), 6) for v in seq.root_rotation_quat[idx]],
            "euler_continuous_deg": {
                "x": round(float(_rad_to_deg(seq.root_euler_xyz_rad[idx, 0])), 3),
                "y": round(float(_rad_to_deg(seq.root_euler_xyz_rad[idx, 1])), 3),
                "z": round(float(_rad_to_deg(seq.root_euler_xyz_rad[idx, 2])), 3),
            },
            "cumulative_rotation_deg": round(
                float(_rad_to_deg(seq.root_cumulative_rotation_rad[idx])), 3
            ),
            "rider_intrinsic": {
                "frame": "rider_intrinsic",
                "facing_horizontal": [
                    round(float(v), 4) for v in seq.rider_facing_horizontal[idx]
                ],
                "lateral_horizontal": [
                    round(float(v), 4) for v in seq.rider_lateral_horizontal[idx]
                ],
                "somersault_rate_deg_s": round(
                    float(_rad_to_deg(seq.intrinsic_somersault_rate_rad_s[idx])), 3
                ),
                "somersault_cumulative_deg": round(
                    float(_rad_to_deg(seq.intrinsic_somersault_cumulative_rad[idx])), 3
                ),
                "sign_convention": "+backflip_direction / -frontflip_direction",
                "note": SOMERSAULT_CONVENTION,
            },
        },
        "center_of_mass": {
            "position_m": [round(float(v), 5) for v in com],
            "velocity_m_s": [round(float(v), 5) for v in com_vel],
            "acceleration_m_s2": [round(float(v), 5) for v in com_acc],
        },
        "contact": {
            "left_foot": bool(seq.contact_left[idx]),
            "right_foot": bool(seq.contact_right[idx]),
            "phase": seq.contact_phase[idx] or None,
        },
        "joints": joint_out,
        "warnings": [
            w
            for w in [
                seq.metadata.get("opensim_warning"),
                (seq.metadata.get("dynamics_metadata") or {}).get("warning"),
                seq.metadata.get("angle_policy"),
            ]
            if w
        ],
    }

    if temporal_window and temporal_window > 0:
        half = float(temporal_window) / 2.0
        result["window"] = get_joint_trajectory_stats_window(seq, t - half, t + half)

    return result


def get_joint_trajectory_stats_window(
    seq: MotionSequence, start_time: float, end_time: float
) -> dict[str, Any]:
    sl = slice_indices(seq.timestamps, start_time, end_time)
    return {
        "start_time": float(seq.timestamps[sl.start]) if sl.start < seq.frame_count else start_time,
        "end_time": float(seq.timestamps[sl.stop - 1]) if sl.stop > sl.start else end_time,
        "frame_count": sl.stop - sl.start,
    }


def get_joint_trajectory(
    seq: MotionSequence,
    joint: str,
    start_time: float,
    end_time: float,
    quantities: list[str] | None = None,
    max_points: int = 120,
    include_plot: bool = False,
) -> dict[str, Any]:
    try:
        name = resolve_joint_name(joint)
    except KeyError:
        return tool_error("UNSUPPORTED_JOINT", f"Unknown joint {joint!r}")

    try:
        t0, t1 = float(start_time), float(end_time)
    except (TypeError, ValueError):
        return tool_error("INVALID_ARGUMENT", "start_time/end_time must be floats")

    if t1 < t0:
        t0, t1 = t1, t0
    if t1 < 0 or t0 > seq.duration_s:
        return tool_error(
            "TIMESTAMP_OUT_OF_RANGE",
            f"interval [{t0}, {t1}] outside duration {seq.duration_s:.4f}",
        )

    quantities = quantities or ["angle", "angular_velocity", "position", "velocity"]
    sl = slice_indices(seq.timestamps, t0, t1)
    if sl.stop <= sl.start:
        # expand to nearest frame
        idx = nearest_index(seq.timestamps, 0.5 * (t0 + t1))
        sl = slice(idx, idx + 1)

    ts = seq.timestamps[sl]
    dt = float(seq.metadata.get("dt") or (1.0 / seq.fps))
    out: dict[str, Any] = {
        "error": False,
        "joint": name,
        "start_time": float(ts[0]),
        "end_time": float(ts[-1]),
        "sample_count": int(ts.size),
        "units": seq.metadata.get("units"),
        "coordinate_system": seq.metadata.get("coordinate_system"),
        "series": {},
        "statistics": {},
    }

    def _downsample(arr: np.ndarray) -> list:
        if arr.shape[0] <= max_points:
            data = arr
            times = ts
        else:
            idx = np.linspace(0, arr.shape[0] - 1, max_points).astype(int)
            data = arr[idx]
            times = ts[idx]
        if data.ndim == 1:
            return {
                "times": [round(float(x), 5) for x in times],
                "values": [round(float(x), 6) for x in data],
            }
        return {
            "times": [round(float(x), 5) for x in times],
            "values": [[round(float(v), 6) for v in row] for row in data],
        }

    if "angle" in quantities or "angular_velocity" in quantities or "angular_acceleration" in quantities:
        if name not in seq.joint_angles_rad:
            out["series"]["angle"] = tool_error(
                "MISSING_KINEMATICS", f"No primary angle for {name}"
            )
        else:
            ang = seq.joint_angles_rad[name][sl]
            ang_deg = np.degrees(ang)
            if "angle" in quantities:
                out["series"]["angle_deg"] = _downsample(ang_deg)
                out["statistics"]["min_angle_deg"] = round(float(np.min(ang_deg)), 3)
                out["statistics"]["max_angle_deg"] = round(float(np.max(ang_deg)), 3)
                out["statistics"]["min_angle_time"] = round(float(ts[int(np.argmin(ang_deg))]), 5)
                out["statistics"]["max_angle_time"] = round(float(ts[int(np.argmax(ang_deg))]), 5)
                out["statistics"]["net_angle_change_deg"] = round(
                    float(ang_deg[-1] - ang_deg[0]), 3
                )
                out["angle_representation"] = "continuous_unwrapped_opensim_dof"
            if name in seq.joint_angles_rad_raw:
                raw_deg = np.degrees(seq.joint_angles_rad_raw[name][sl])
                out["series"]["opensim_dof_raw_deg"] = _downsample(raw_deg)
            if name == "pelvis":
                eul = np.degrees(seq.root_euler_xyz_rad[sl])
                out["series"]["root_euler_x_continuous_deg"] = _downsample(eul[:, 0])
                out["series"]["root_euler_y_continuous_deg"] = _downsample(eul[:, 1])
                out["series"]["root_euler_z_continuous_deg"] = _downsample(eul[:, 2])
                cum = np.degrees(seq.root_cumulative_rotation_rad[sl])
                out["series"]["root_cumulative_rotation_deg"] = _downsample(cum)
                soma_rate = np.degrees(seq.intrinsic_somersault_rate_rad_s[sl])
                soma_cum = np.degrees(seq.intrinsic_somersault_cumulative_rad[sl])
                out["series"]["intrinsic_somersault_rate_deg_s"] = _downsample(soma_rate)
                out["series"]["intrinsic_somersault_cumulative_deg"] = _downsample(soma_cum)
                soma_net = float(soma_cum[-1] - soma_cum[0])
                out["statistics"]["root_euler_x_net_change_deg"] = round(
                    float(eul[-1, 0] - eul[0, 0]), 3
                )
                out["statistics"]["root_cumulative_rotation_net_change_deg"] = round(
                    float(cum[-1] - cum[0]), 3
                )
                out["statistics"]["intrinsic_somersault_net_change_deg"] = round(soma_net, 3)
                out["statistics"]["flip_direction_hint"] = somersault_direction_label(
                    np.deg2rad(soma_net)
                )
                out["statistics"]["preferred_for_flip_extent"] = (
                    "root_euler_*_continuous_deg / root_cumulative_rotation_deg (world)"
                )
                out["statistics"]["preferred_for_front_vs_back"] = (
                    "intrinsic_somersault_* (+backflip / −frontflip)"
                )
                out["root_euler_frame"] = "world"
                out["somersault_frame"] = "rider_intrinsic"
                out["somersault_convention"] = SOMERSAULT_CONVENTION
                out["orientation_note"] = (
                    "World root_euler_* measures flip extent but its sign is not "
                    "rider front/back. Use intrinsic_somersault_net_change_deg and "
                    "flip_direction_hint for frontflip vs backflip. "
                    "OpenSim pelvis_tilt may stall near ±180°."
                )
            ang_vel = np.gradient(ang, dt)
            ang_vel_deg = np.degrees(ang_vel)
            if "angular_velocity" in quantities:
                out["series"]["angular_velocity_deg_s"] = _downsample(ang_vel_deg)
                out["statistics"]["peak_angular_velocity_deg_s"] = round(
                    float(ang_vel_deg[np.argmax(np.abs(ang_vel_deg))]), 3
                )
                out["statistics"]["peak_angular_velocity_time"] = round(
                    float(ts[int(np.argmax(np.abs(ang_vel_deg)))]), 5
                )
            if "angular_acceleration" in quantities:
                ang_acc = np.gradient(ang_vel, dt)
                out["series"]["angular_acceleration_rad_s2"] = _downsample(ang_acc)

    if name in seq.joint_positions and any(
        q in quantities for q in ("position", "velocity", "acceleration")
    ):
        pos = seq.joint_positions[name][sl]
        if "position" in quantities:
            out["series"]["position_m"] = _downsample(pos)
        vel = velocity_of(seq.joint_positions[name], dt)[sl]
        if "velocity" in quantities:
            out["series"]["velocity_m_s"] = _downsample(vel)
            speed = np.linalg.norm(vel, axis=1)
            out["statistics"]["peak_velocity_m_s"] = round(float(np.max(speed)), 5)
            out["statistics"]["peak_velocity_time"] = round(float(ts[int(np.argmax(speed))]), 5)
        if "acceleration" in quantities:
            acc = acceleration_of(seq.joint_positions[name], dt)[sl]
            out["series"]["acceleration_m_s2"] = _downsample(acc)
            amag = np.linalg.norm(acc, axis=1)
            out["statistics"]["peak_acceleration_m_s2"] = round(float(np.max(amag)), 5)
            out["statistics"]["peak_acceleration_time"] = round(
                float(ts[int(np.argmax(amag))]), 5
            )

    if include_plot:
        from motion_agent.rendering.plots import plot_time_series

        plot_series: dict[str, np.ndarray] = {}
        if name in seq.joint_angles_rad:
            plot_series["opensim_dof_unwrapped_deg"] = np.degrees(seq.joint_angles_rad[name][sl])
        if name == "pelvis":
            plot_series["root_euler_x_continuous_deg"] = np.degrees(seq.root_euler_xyz_rad[sl, 0])
            plot_series["intrinsic_somersault_cumulative_deg"] = np.degrees(
                seq.intrinsic_somersault_cumulative_rad[sl]
            )
        if plot_series:
            out["mime_type"] = "image/png"
            out["image_base64"] = plot_time_series(
                ts,
                plot_series,
                title=f"{name} orientation (continuous)",
                ylabel="deg",
            )

    return out


def get_com_trajectory(
    seq: MotionSequence,
    start_time: float,
    end_time: float,
    max_points: int = 120,
    include_plot: bool = False,
) -> dict[str, Any]:
    try:
        t0, t1 = float(start_time), float(end_time)
    except (TypeError, ValueError):
        return tool_error("INVALID_ARGUMENT", "start_time/end_time must be floats")
    if t1 < t0:
        t0, t1 = t1, t0
    sl = slice_indices(seq.timestamps, t0, t1)
    if sl.stop <= sl.start:
        idx = nearest_index(seq.timestamps, 0.5 * (t0 + t1))
        sl = slice(idx, idx + 1)
    ts = seq.timestamps[sl]
    dt = float(seq.metadata.get("dt") or (1.0 / seq.fps))
    pos = seq.com_position[sl]
    vel = velocity_of(seq.com_position, dt)[sl]
    acc = acceleration_of(seq.com_position, dt)[sl]
    vy = vel[:, 1]
    ay = acc[:, 1]

    def _ds(arr: np.ndarray) -> dict:
        if arr.shape[0] <= max_points:
            data, times = arr, ts
        else:
            idx = np.linspace(0, arr.shape[0] - 1, max_points).astype(int)
            data, times = arr[idx], ts[idx]
        return {
            "times": [round(float(x), 5) for x in times],
            "values": [[round(float(v), 6) for v in row] for row in data],
        }

    payload = {
        "error": False,
        "start_time": float(ts[0]),
        "end_time": float(ts[-1]),
        "coordinate_system": seq.metadata.get("coordinate_system"),
        "units": {"position": "m", "velocity": "m/s", "acceleration": "m/s^2"},
        "position_m": _ds(pos),
        "velocity_m_s": _ds(vel),
        "acceleration_m_s2": _ds(acc),
        "statistics": {
            "max_vertical_velocity_m_s": round(float(np.max(vy)), 5),
            "max_vertical_velocity_time": round(float(ts[int(np.argmax(vy))]), 5),
            "min_vertical_velocity_m_s": round(float(np.min(vy)), 5),
            "min_vertical_velocity_time": round(float(ts[int(np.argmin(vy))]), 5),
            "max_vertical_acceleration_m_s2": round(float(np.max(ay)), 5),
            "max_vertical_acceleration_time": round(float(ts[int(np.argmax(ay))]), 5),
            "max_com_height_m": round(float(np.max(pos[:, 1])), 5),
            "max_com_height_time": round(float(ts[int(np.argmax(pos[:, 1]))]), 5),
            "min_com_height_m": round(float(np.min(pos[:, 1])), 5),
            "min_com_height_time": round(float(ts[int(np.argmin(pos[:, 1]))]), 5),
        },
    }
    if include_plot:
        from motion_agent.rendering.plots import plot_com_height

        payload["mime_type"] = "image/png"
        payload["image_base64"] = plot_com_height(ts, pos[:, 1], vy)
    return payload


def get_contact_state(
    seq: MotionSequence,
    start_time: float | None = None,
    end_time: float | None = None,
) -> dict[str, Any]:
    if not seq.has_dynamics() and not np.any(seq.contact_left | seq.contact_right):
        # still return zeros with warning
        pass
    t0 = 0.0 if start_time is None else float(start_time)
    t1 = seq.duration_s if end_time is None else float(end_time)
    if t1 < t0:
        t0, t1 = t1, t0
    sl = slice_indices(seq.timestamps, t0, t1)
    if sl.stop <= sl.start:
        return tool_error("TIMESTAMP_OUT_OF_RANGE", "empty contact window")

    ts = seq.timestamps[sl]
    left = seq.contact_left[sl]
    right = seq.contact_right[sl]

    def _intervals(mask: np.ndarray) -> list[dict[str, float]]:
        intervals: list[dict[str, float]] = []
        if mask.size == 0:
            return intervals
        padded = np.concatenate([[False], mask, [False]])
        diff = np.diff(padded.astype(int))
        starts = np.where(diff == 1)[0]
        ends = np.where(diff == -1)[0]
        for s, e in zip(starts, ends):
            intervals.append(
                {
                    "start_time": round(float(ts[s]), 5),
                    "end_time": round(float(ts[min(e - 1, len(ts) - 1)]), 5),
                }
            )
        return intervals

    transitions = []
    for i in range(1, len(left)):
        if left[i] != left[i - 1]:
            transitions.append(
                {
                    "timestamp": round(float(ts[i]), 5),
                    "side": "left",
                    "to_contact": bool(left[i]),
                }
            )
        if right[i] != right[i - 1]:
            transitions.append(
                {
                    "timestamp": round(float(ts[i]), 5),
                    "side": "right",
                    "to_contact": bool(right[i]),
                }
            )

    return {
        "error": False,
        "start_time": float(ts[0]),
        "end_time": float(ts[-1]),
        "left_foot_contact_fraction": round(float(np.mean(left)), 4),
        "right_foot_contact_fraction": round(float(np.mean(right)), 4),
        "left_contact_intervals": _intervals(left),
        "right_contact_intervals": _intervals(right),
        "transitions": transitions[:200],
        "note": "Contacts estimated from monocular pipeline / OpenSim estimated GRF, not force plates.",
        "warning": (seq.metadata.get("dynamics_metadata") or {}).get("warning"),
    }
