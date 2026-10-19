"""OpenSim estimated-dynamics accessors."""

from __future__ import annotations

from typing import Any

import numpy as np

from motion_agent.motion.sequence import MotionSequence
from motion_agent.motion.smplx_mapping import DYNAMICS_JOINT_TO_SMPL_X, resolve_joint_name
from motion_agent.motion.synchronization import slice_indices
from motion_agent.storage.errors import tool_error

POWER_CONVENTION = (
    "P = flexionMomentNm * angularVelocityRadS. "
    "Positive power ≈ energy generation along the flexion DOF; "
    "negative power ≈ absorption. Sign follows OpenSim flexion moment and "
    "angular-velocity conventions for the Rajagopal model — do not assume "
    "positive always means concentric propulsion without checking the joint."
)


def _trapz(y: np.ndarray, x: np.ndarray) -> float:
    if hasattr(np, "trapezoid"):
        return float(np.trapezoid(y, x))
    return float(np.trapz(y, x))  # numpy < 2


def _require_dynamics(seq: MotionSequence) -> dict[str, Any] | None:
    if not seq.has_dynamics():
        return tool_error(
            "MISSING_DYNAMICS",
            "OpenSim dynamics are unavailable for this sequence.",
        )
    return None


def get_dynamics_summary(
    seq: MotionSequence,
    start_time: float,
    end_time: float,
) -> dict[str, Any]:
    err = _require_dynamics(seq)
    if err:
        return err
    try:
        t0, t1 = float(start_time), float(end_time)
    except (TypeError, ValueError):
        return tool_error("INVALID_ARGUMENT", "start_time/end_time must be floats")
    if t1 < t0:
        t0, t1 = t1, t0
    sl = slice_indices(seq.timestamps, t0, t1)
    if sl.stop <= sl.start:
        return tool_error("TIMESTAMP_OUT_OF_RANGE", "empty dynamics window")

    ts = seq.timestamps[sl]
    joint_summaries: dict[str, Any] = {}
    peak_moment_rank: list[tuple[str, float]] = []
    peak_pos_power: list[tuple[str, float]] = []
    peak_neg_power: list[tuple[str, float]] = []

    for smplx in DYNAMICS_JOINT_TO_SMPL_X.values():
        mom = seq.joint_flexion_moment_nm[smplx][sl]
        power = seq.joint_power_w[smplx][sl]
        conf = seq.confidence.get(smplx, np.zeros_like(mom))[sl]
        i_mom = int(np.argmax(np.abs(mom)))
        i_pos = int(np.argmax(power))
        i_neg = int(np.argmin(power))
        peak_m = float(mom[i_mom])
        peak_p = float(power[i_pos])
        peak_n = float(power[i_neg])
        role = None
        # Only assign semantic roles when magnitudes are meaningful
        if abs(peak_p) > 20 and peak_p >= abs(peak_n):
            role = "propulsion_candidate"
        elif abs(peak_n) > 20 and abs(peak_n) > peak_p:
            role = "load_absorption_candidate"
        joint_summaries[smplx] = {
            "peak_moment_nm": round(peak_m, 3),
            "peak_moment_time": round(float(ts[i_mom]), 5),
            "peak_positive_power_w": round(peak_p, 3),
            "peak_positive_power_time": round(float(ts[i_pos]), 5),
            "peak_negative_power_w": round(peak_n, 3),
            "peak_negative_power_time": round(float(ts[i_neg]), 5),
            "mean_abs_moment_nm": round(float(np.mean(np.abs(mom))), 3),
            "mean_confidence": round(float(np.mean(conf)), 4),
            "role": role,
        }
        peak_moment_rank.append((smplx, abs(peak_m)))
        peak_pos_power.append((smplx, peak_p))
        peak_neg_power.append((smplx, peak_n))

    grf_mag = np.linalg.norm(seq.grf_total_n[sl], axis=1)
    i_grf = int(np.argmax(grf_mag))

    peak_moment_rank.sort(key=lambda x: x[1], reverse=True)
    peak_pos_power.sort(key=lambda x: x[1], reverse=True)
    peak_neg_power.sort(key=lambda x: x[1])  # most negative first

    return {
        "error": False,
        "start_time": float(ts[0]),
        "end_time": float(ts[-1]),
        "units": {"moment": "N·m", "power": "W", "force": "N"},
        "power_convention": POWER_CONVENTION,
        "coordinate_system": seq.metadata.get("coordinate_system"),
        "joints": joint_summaries,
        "largest_moments": [
            {"joint": j, "peak_abs_moment_nm": round(v, 3)} for j, v in peak_moment_rank[:4]
        ],
        "largest_positive_power": [
            {"joint": j, "peak_power_w": round(v, 3)} for j, v in peak_pos_power[:4]
        ],
        "largest_negative_power": [
            {"joint": j, "peak_power_w": round(v, 3)} for j, v in peak_neg_power[:4]
        ],
        "peak_ground_reaction_force": {
            "magnitude_n": round(float(grf_mag[i_grf]), 3),
            "force_n": [round(float(x), 3) for x in seq.grf_total_n[sl][i_grf]],
            "timestamp": round(float(ts[i_grf]), 5),
            "body_weight_ratio": round(
                float(grf_mag[i_grf] / max(float((seq.metadata.get("dynamics_metadata") or {}).get("bodyWeightN") or 1.0), 1e-6)),
                4,
            ),
        },
        "confidence_ceiling": (seq.metadata.get("dynamics_metadata") or {}).get(
            "confidenceCeiling"
        ),
        "warning": (seq.metadata.get("dynamics_metadata") or {}).get("warning"),
    }


def get_joint_dynamics(
    seq: MotionSequence,
    joint: str,
    start_time: float,
    end_time: float,
    quantities: list[str] | None = None,
    max_points: int = 120,
) -> dict[str, Any]:
    err = _require_dynamics(seq)
    if err:
        return err
    try:
        name = resolve_joint_name(joint)
    except KeyError:
        return tool_error("UNSUPPORTED_JOINT", f"Unknown joint {joint!r}")
    if name not in seq.joint_flexion_moment_nm:
        return tool_error(
            "MISSING_DYNAMICS",
            f"No OpenSim dynamics for joint {name}. Available: {sorted(seq.joint_flexion_moment_nm)}",
        )
    try:
        t0, t1 = float(start_time), float(end_time)
    except (TypeError, ValueError):
        return tool_error("INVALID_ARGUMENT", "start_time/end_time must be floats")
    if t1 < t0:
        t0, t1 = t1, t0
    sl = slice_indices(seq.timestamps, t0, t1)
    if sl.stop <= sl.start:
        return tool_error("TIMESTAMP_OUT_OF_RANGE", "empty dynamics window")

    quantities = quantities or ["moment", "reaction_force", "power"]
    ts = seq.timestamps[sl]

    def _ds(arr: np.ndarray) -> dict:
        if arr.shape[0] <= max_points:
            data, times = arr, ts
        else:
            idx = np.linspace(0, arr.shape[0] - 1, max_points).astype(int)
            data, times = arr[idx], ts[idx]
        if data.ndim == 1:
            return {
                "times": [round(float(x), 5) for x in times],
                "values": [round(float(x), 6) for x in data],
            }
        return {
            "times": [round(float(x), 5) for x in times],
            "values": [[round(float(v), 6) for v in row] for row in data],
        }

    out: dict[str, Any] = {
        "error": False,
        "joint": name,
        "start_time": float(ts[0]),
        "end_time": float(ts[-1]),
        "units": {"moment": "N·m", "force": "N", "power": "W", "angular_velocity": "rad/s"},
        "sign_convention": (
            "flexionMomentNm: OpenSim flexion DOF moment. "
            "angularVelocityRadS: flexion-coordinate rate from dynamics export."
        ),
        "power_convention": POWER_CONVENTION,
        "coordinate_system": seq.metadata.get("coordinate_system"),
        "series": {},
        "extrema": {},
        "mean_confidence": round(float(np.mean(seq.confidence[name][sl])), 4),
        "warning": (seq.metadata.get("dynamics_metadata") or {}).get("warning"),
    }
    if "moment" in quantities:
        mom = seq.joint_flexion_moment_nm[name][sl]
        out["series"]["flexion_moment_nm"] = _ds(mom)
        i = int(np.argmax(np.abs(mom)))
        out["extrema"]["peak_flexion_moment_nm"] = round(float(mom[i]), 3)
        out["extrema"]["peak_flexion_moment_time"] = round(float(ts[i]), 5)
    if "power" in quantities:
        power = seq.joint_power_w[name][sl]
        out["series"]["power_w"] = _ds(power)
        out["extrema"]["peak_positive_power_w"] = round(float(np.max(power)), 3)
        out["extrema"]["peak_positive_power_time"] = round(float(ts[int(np.argmax(power))]), 5)
        out["extrema"]["peak_negative_power_w"] = round(float(np.min(power)), 3)
        out["extrema"]["peak_negative_power_time"] = round(float(ts[int(np.argmin(power))]), 5)
    if "reaction_force" in quantities and name in seq.joint_reaction_force_n:
        force = seq.joint_reaction_force_n[name][sl]
        out["series"]["reaction_force_n"] = _ds(force)
        mag = np.linalg.norm(force, axis=1)
        out["extrema"]["peak_reaction_force_n"] = round(float(np.max(mag)), 3)
        out["extrema"]["peak_reaction_force_time"] = round(float(ts[int(np.argmax(mag))]), 5)
    if name in seq.joint_angular_velocity_rad_s:
        out["series"]["angular_velocity_rad_s"] = _ds(seq.joint_angular_velocity_rad_s[name][sl])
    return out


def get_ground_reaction_forces(
    seq: MotionSequence,
    start_time: float,
    end_time: float,
    max_points: int = 120,
) -> dict[str, Any]:
    err = _require_dynamics(seq)
    if err:
        return err
    try:
        t0, t1 = float(start_time), float(end_time)
    except (TypeError, ValueError):
        return tool_error("INVALID_ARGUMENT", "start_time/end_time must be floats")
    if t1 < t0:
        t0, t1 = t1, t0
    sl = slice_indices(seq.timestamps, t0, t1)
    if sl.stop <= sl.start:
        return tool_error("TIMESTAMP_OUT_OF_RANGE", "empty GRF window")
    ts = seq.timestamps[sl]

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

    total = seq.grf_total_n[sl]
    mag = np.linalg.norm(total, axis=1)
    i = int(np.argmax(mag))
    return {
        "error": False,
        "start_time": float(ts[0]),
        "end_time": float(ts[-1]),
        "units": {"force": "N"},
        "coordinate_system": seq.metadata.get("coordinate_system"),
        "total_force_n": _ds(total),
        "left_force_n": _ds(seq.grf_left_n[sl]),
        "right_force_n": _ds(seq.grf_right_n[sl]),
        "left_active_fraction": round(float(np.mean(seq.grf_left_active[sl])), 4),
        "right_active_fraction": round(float(np.mean(seq.grf_right_active[sl])), 4),
        "peak_total": {
            "magnitude_n": round(float(mag[i]), 3),
            "force_n": [round(float(x), 3) for x in total[i]],
            "timestamp": round(float(ts[i]), 5),
        },
        "note": "Center of pressure is not exposed in the current estimated-dynamics export.",
        "warning": (seq.metadata.get("dynamics_metadata") or {}).get("warning"),
    }


def get_joint_power(
    seq: MotionSequence,
    start_time: float,
    end_time: float,
    joints: list[str] | None = None,
) -> dict[str, Any]:
    err = _require_dynamics(seq)
    if err:
        return err
    try:
        t0, t1 = float(start_time), float(end_time)
    except (TypeError, ValueError):
        return tool_error("INVALID_ARGUMENT", "start_time/end_time must be floats")
    if t1 < t0:
        t0, t1 = t1, t0
    sl = slice_indices(seq.timestamps, t0, t1)
    if sl.stop <= sl.start:
        return tool_error("TIMESTAMP_OUT_OF_RANGE", "empty power window")
    ts = seq.timestamps[sl]

    if joints:
        names = []
        for j in joints:
            try:
                names.append(resolve_joint_name(j))
            except KeyError:
                return tool_error("UNSUPPORTED_JOINT", f"Unknown joint {j!r}")
    else:
        names = list(seq.joint_power_w.keys())

    joint_out: dict[str, Any] = {}
    for name in names:
        if name not in seq.joint_power_w:
            joint_out[name] = tool_error("MISSING_DYNAMICS", f"No power for {name}")
            continue
        p = seq.joint_power_w[name][sl]
        joint_out[name] = {
            "peak_positive_power_w": round(float(np.max(p)), 3),
            "peak_positive_power_time": round(float(ts[int(np.argmax(p))]), 5),
            "peak_negative_power_w": round(float(np.min(p)), 3),
            "peak_negative_power_time": round(float(ts[int(np.argmin(p))]), 5),
            "mean_positive_power_w": round(float(np.mean(np.clip(p, 0, None))), 3),
            "mean_negative_power_w": round(float(np.mean(np.clip(p, None, 0))), 3),
            "net_work_proxy_j": round(float(_trapz(p, ts)), 3),
        }

    return {
        "error": False,
        "start_time": float(ts[0]),
        "end_time": float(ts[-1]),
        "units": {"power": "W", "work_proxy": "J"},
        "power_convention": POWER_CONVENTION,
        "joints": joint_out,
        "warning": (seq.metadata.get("dynamics_metadata") or {}).get("warning"),
    }
