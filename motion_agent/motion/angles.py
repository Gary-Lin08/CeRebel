"""Angle unwrapping and continuous root orientation helpers.

OpenSim IK generalized coordinates are principal values that can stall or reverse
near ±π even when the body continues rotating (e.g. flips). For large aerial
reorientations:

- Prefer continuous root Euler / cumulative rotation (WORLD / OpenSim ground) for
  flip *extent*.
- Prefer rider-intrinsic somersault (about rightward = facing × up) for
  frontflip vs backflip. Do not infer front vs back from world Euler sign alone.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def unwrap_radians(angles: np.ndarray, discont: float = np.pi) -> np.ndarray:
    """Continuously unwrap a 1-D angle series (radians)."""
    arr = np.asarray(angles, dtype=np.float64)
    if arr.size == 0:
        return arr.copy()
    return np.unwrap(arr, discont=discont)


def make_quaternions_continuous(quats_xyzw: np.ndarray) -> np.ndarray:
    """Flip quaternion signs so consecutive frames share the same hemisphere."""
    q = np.asarray(quats_xyzw, dtype=np.float64).copy()
    if q.ndim != 2 or q.shape[1] != 4 or q.shape[0] == 0:
        return q
    for i in range(1, q.shape[0]):
        if float(np.dot(q[i - 1], q[i])) < 0.0:
            q[i] *= -1.0
    return q


def quat_xyzw_to_euler_xyz(quats_xyzw: np.ndarray) -> np.ndarray:
    """Convert xyzw quaternions to XYZ Euler angles (radians) in the WORLD frame.

    These express body orientation relative to OpenSim ground, not rider-intrinsic
    somersault. Use ``intrinsic_somersault_from_root`` for front vs back flips.
    """
    q = np.asarray(quats_xyzw, dtype=np.float64)
    try:
        from scipy.spatial.transform import Rotation as R

        return R.from_quat(q).as_euler("xyz", degrees=False)
    except Exception:
        # Manual XYZ intrinsic conversion for xyzw quaternions.
        x, y, z, w = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
        # roll (x)
        sinr_cosp = 2.0 * (w * x + y * z)
        cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
        roll = np.arctan2(sinr_cosp, cosr_cosp)
        # pitch (y)
        sinp = 2.0 * (w * y - z * x)
        sinp = np.clip(sinp, -1.0, 1.0)
        pitch = np.arcsin(sinp)
        # yaw (z)
        siny_cosp = 2.0 * (w * z + x * y)
        cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
        yaw = np.arctan2(siny_cosp, cosy_cosp)
        return np.column_stack([roll, pitch, yaw])


def continuous_euler_xyz_from_quaternions(quats_xyzw: np.ndarray) -> np.ndarray:
    """Hemisphere-continuous quaternions → unwrapped XYZ Euler (radians)."""
    q = make_quaternions_continuous(quats_xyzw)
    eul = quat_xyzw_to_euler_xyz(q)
    out = np.empty_like(eul)
    for ax in range(3):
        out[:, ax] = unwrap_radians(eul[:, ax])
    return out


def cumulative_rotation_from_quaternions(quats_xyzw: np.ndarray) -> np.ndarray:
    """Per-frame cumulative rotation magnitude (radians) from t=0.

    Uses hemisphere-fixed consecutive relative quaternions and integrates the
    unsigned rotation angle with sign from the relative quaternion to reduce
    shortest-path folding across large aerial rotations.
    """
    q = make_quaternions_continuous(quats_xyzw)
    n = q.shape[0]
    cum = np.zeros(n, dtype=np.float64)
    if n < 2:
        return cum

    def _qmul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        x1, y1, z1, w1 = a
        x2, y2, z2, w2 = b
        return np.array(
            [
                w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
                w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            ],
            dtype=np.float64,
        )

    def _qconj(a: np.ndarray) -> np.ndarray:
        return np.array([-a[0], -a[1], -a[2], a[3]], dtype=np.float64)

    total = 0.0
    prev_delta = 0.0
    for i in range(1, n):
        dq = _qmul(_qconj(q[i - 1]), q[i])
        # Keep dq in the positive-w hemisphere for a continuous small-step angle,
        # then restore continuity against the previous step if needed.
        if dq[3] < 0.0:
            dq = -dq
        vnorm = float(np.linalg.norm(dq[:3]))
        delta = 2.0 * float(np.arctan2(vnorm, float(dq[3])))
        # If the step suddenly reverses relative to previous large motion, prefer
        # the long-way supplement (rare; helps near singularities).
        if i > 1 and abs(delta) > 1e-6 and abs(prev_delta) > 0.25:
            if delta * prev_delta < 0 and abs(delta) > 0.5 and abs(abs(delta) - abs(prev_delta)) > 0.4:
                # flip sign to keep spinning direction
                delta = -delta
        total += delta
        prev_delta = delta
        cum[i] = total
    return cum


SOMERSAULT_CONVENTION = (
    "Rider-intrinsic somersault is rotation about the horizontal rightward axis "
    "(facing × world_up). Sign: positive = backflip direction (head going rearward "
    "relative to facing); negative = frontflip direction (head diving forward). "
    "Facing is horizontal COM velocity when |v_h| is sufficient, otherwise the "
    "pelvis body +X axis projected onto the ground plane. "
    "root_euler_* values are OpenSim WORLD/ground frame (Y-up), NOT rider-intrinsic — "
    "do not infer frontflip vs backflip from world Euler sign alone."
)


def _qmul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    x1, y1, z1, w1 = a
    x2, y2, z2, w2 = b
    return np.array(
        [
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        ],
        dtype=np.float64,
    )


def _qconj(a: np.ndarray) -> np.ndarray:
    return np.array([-a[0], -a[1], -a[2], a[3]], dtype=np.float64)


def quat_xyzw_to_rotation_matrix(q: np.ndarray) -> np.ndarray:
    """Body→world rotation matrix for an xyzw quaternion."""
    x, y, z, w = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def angular_velocity_world_from_quaternions(
    quats_xyzw: np.ndarray, dt: float
) -> np.ndarray:
    """World-frame angular velocity (rad/s) via ω = 2 * q̇ ⊗ q*."""
    q = make_quaternions_continuous(quats_xyzw)
    n = q.shape[0]
    out = np.zeros((n, 3), dtype=np.float64)
    if n == 0 or dt <= 0:
        return out
    qd = np.gradient(q, dt, axis=0)
    for i in range(n):
        ow = 2.0 * _qmul(qd[i], _qconj(q[i]))
        out[i] = ow[:3]
    return out


def rider_facing_horizontal(
    quats_xyzw: np.ndarray,
    com_position: np.ndarray,
    dt: float,
    *,
    min_speed_m_s: float = 0.25,
) -> np.ndarray:
    """Per-frame horizontal facing unit vectors in world (Y-up)."""
    q = make_quaternions_continuous(quats_xyzw)
    n = q.shape[0]
    facing = np.zeros((n, 3), dtype=np.float64)
    if n == 0:
        return facing
    com_vel = np.gradient(np.asarray(com_position, dtype=np.float64), dt, axis=0)
    last = np.array([1.0, 0.0, 0.0], dtype=np.float64)
    for i in range(n):
        v_h = np.array([com_vel[i, 0], 0.0, com_vel[i, 2]], dtype=np.float64)
        speed = float(np.linalg.norm(v_h))
        if speed >= min_speed_m_s:
            f = v_h / speed
        else:
            R = quat_xyzw_to_rotation_matrix(q[i])
            f = R[:, 0].copy()  # pelvis body +X in world
            f[1] = 0.0
            nrm = float(np.linalg.norm(f))
            f = f / nrm if nrm > 1e-8 else last.copy()
        # Keep facing hemisphere continuous vs previous
        if float(np.dot(f, last)) < 0.0:
            f = -f
        last = f
        facing[i] = f
    # Light temporal smooth
    if n >= 5:
        kernel = np.ones(5) / 5.0
        for ax in (0, 2):
            facing[:, ax] = np.convolve(facing[:, ax], kernel, mode="same")
        for i in range(n):
            facing[i, 1] = 0.0
            nrm = float(np.linalg.norm(facing[i]))
            if nrm > 1e-8:
                facing[i] /= nrm
    return facing


def intrinsic_somersault_from_root(
    quats_xyzw: np.ndarray,
    com_position: np.ndarray,
    dt: float,
) -> dict[str, np.ndarray]:
    """Rider-intrinsic somersault rate/cumulative about rightward axis.

    Returns facing, lateral (rightward), rate (rad/s), cumulative (rad from t=0).
    Positive rate/cumulative ⇒ backflip direction; negative ⇒ frontflip.
    """
    q = make_quaternions_continuous(quats_xyzw)
    n = q.shape[0]
    facing = rider_facing_horizontal(q, com_position, dt)
    lateral = np.zeros((n, 3), dtype=np.float64)
    up = np.array([0.0, 1.0, 0.0], dtype=np.float64)
    for i in range(n):
        # Rightward = facing × up (Y-up OpenSim). Backflip is +ω about this axis.
        lat = np.cross(facing[i], up)
        nrm = float(np.linalg.norm(lat))
        lateral[i] = lat / nrm if nrm > 1e-8 else np.array([0.0, 0.0, 1.0])

    w_world = angular_velocity_world_from_quaternions(q, dt)
    rate = np.einsum("ij,ij->i", w_world, lateral)
    cum = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        cum[i] = cum[i - 1] + 0.5 * (rate[i] + rate[i - 1]) * dt
    return {
        "facing_horizontal": facing,
        "lateral_horizontal": lateral,
        "somersault_rate_rad_s": rate,
        "somersault_cumulative_rad": cum,
    }


def somersault_direction_label(net_rad: float, *, min_abs_deg: float = 45.0) -> str:
    deg = float(np.degrees(net_rad))
    if abs(deg) < min_abs_deg:
        return "ambiguous"
    return "backflip_direction" if deg > 0 else "frontflip_direction"


def wrap_risk_metadata(
    dof_rad: float,
    euler_match_rad: float | None = None,
    *,
    near_pi_tol: float = 0.35,
) -> dict[str, Any]:
    """Flag when an OpenSim DOF is near ±π and may be an unreliable flip signal."""
    principal = float(np.arctan2(np.sin(dof_rad), np.cos(dof_rad)))
    near = abs(abs(principal) - np.pi) < near_pi_tol or abs(abs(principal) - np.pi / 2) < 0.15
    mismatch = False
    if euler_match_rad is not None:
        # Compare modulo 2π after aligning
        d = float(dof_rad - euler_match_rad)
        d = (d + np.pi) % (2 * np.pi) - np.pi
        mismatch = abs(d) > np.deg2rad(35.0)
    risky = bool(near or mismatch)
    return {
        "near_singularity_or_wrap": risky,
        "opensim_dof_principal_deg": round(float(np.degrees(principal)), 3),
        "note": (
            "OpenSim DOF angles can stall or reverse near ±180° even when the body "
            "continues rotating. Prefer root_euler_continuous_deg / "
            "root_cumulative_rotation_deg for flip extent (WORLD frame). Prefer "
            "intrinsic_somersault_* for frontflip vs backflip (+back / −front)."
            if risky
            else None
        ),
    }
