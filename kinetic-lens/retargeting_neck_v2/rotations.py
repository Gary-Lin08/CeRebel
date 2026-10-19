"""Rotation helpers. All conversions use matrices or xyzw quaternions — never Euler for IK."""

from __future__ import annotations

import math

import numpy as np

P_SOMA_YUP_TO_OPENSIM = np.array(
    [[0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]],
    dtype=np.float64,
)


def rodrigues(axis_angle: np.ndarray) -> np.ndarray:
    r = np.asarray(axis_angle, dtype=np.float64).reshape(3)
    theta = float(np.linalg.norm(r))
    if theta < 1e-12:
        return np.eye(3)
    k = r / theta
    kx = np.array(
        [[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]],
        dtype=np.float64,
    )
    return np.eye(3) + math.sin(theta) * kx + (1.0 - math.cos(theta)) * (kx @ kx)


def batch_rodrigues(axis_angles: np.ndarray) -> np.ndarray:
    aa = np.asarray(axis_angles, dtype=np.float64)
    out = np.empty(aa.shape[:-1] + (3, 3), dtype=np.float64)
    it = np.ndindex(aa.shape[:-1])
    for idx in it:
        out[idx] = rodrigues(aa[idx])
    return out


def orthonormalize(matrix: np.ndarray) -> np.ndarray:
    u, _, vt = np.linalg.svd(np.asarray(matrix, dtype=np.float64))
    rot = u @ vt
    if np.linalg.det(rot) < 0.0:
        u = u.copy()
        u[:, -1] *= -1.0
        rot = u @ vt
    return rot


def rotation_from_y_z(y_axis: np.ndarray, z_hint: np.ndarray) -> np.ndarray:
    """OpenSim-like body frame: +Y superior, +Z right, +X forward."""
    y = np.asarray(y_axis, dtype=np.float64)
    yn = float(np.linalg.norm(y))
    y = y / yn if yn > 1e-8 else np.array([0.0, 1.0, 0.0])
    z = np.asarray(z_hint, dtype=np.float64)
    z = z - y * float(np.dot(z, y))
    zn = float(np.linalg.norm(z))
    z = z / zn if zn > 1e-8 else np.array([0.0, 0.0, 1.0])
    x = np.cross(y, z)
    xn = float(np.linalg.norm(x))
    x = x / xn if xn > 1e-8 else np.array([1.0, 0.0, 0.0])
    z = np.cross(x, y)
    rot = np.column_stack([x, y, z])
    return orthonormalize(rot)


def so3_log_norm(relative: np.ndarray) -> float:
    r = orthonormalize(relative)
    cos_t = float(np.clip((np.trace(r) - 1.0) * 0.5, -1.0, 1.0))
    return float(math.acos(cos_t))


def geodesic_deg(r_a: np.ndarray, r_b: np.ndarray) -> float:
    return math.degrees(so3_log_norm(r_a.T @ r_b))


def quat_xyzw_from_matrix(rot: np.ndarray) -> np.ndarray:
    m = np.asarray(rot, dtype=np.float64)
    trace = float(m[0, 0] + m[1, 1] + m[2, 2])
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w, x, y, z = 0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        w, x, y, z = (m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        w, x, y, z = (m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        w, x, y, z = (m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s
    q = np.array([x, y, z, w], dtype=np.float64)
    q /= max(float(np.linalg.norm(q)), 1e-12)
    return q


def matrix_from_quat_xyzw(q: np.ndarray) -> np.ndarray:
    x, y, z, w = np.asarray(q, dtype=np.float64)
    n = float(np.linalg.norm([x, y, z, w]))
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def make_quaternions_continuous(quats: np.ndarray) -> np.ndarray:
    q = np.array(quats, dtype=np.float64, copy=True)
    for t in range(1, len(q)):
        if float(np.dot(q[t], q[t - 1])) < 0.0:
            q[t] *= -1.0
    return q


def forward_kinematics_rotations(local_mats: np.ndarray, parents: list[int]) -> np.ndarray:
    """local_mats: (T, J, 3, 3) parent-relative. Returns world rotations."""
    t_count, j_count = local_mats.shape[:2]
    world = np.zeros_like(local_mats)
    for j in range(j_count):
        p = parents[j]
        if p < 0:
            world[:, j] = local_mats[:, j]
        else:
            world[:, j] = world[:, p] @ local_mats[:, j]
    return world


def apply_world_linear_map(rotations: np.ndarray, linear: np.ndarray) -> np.ndarray:
    """R' = M @ R for a world-frame linear map M (det ±1)."""
    m = np.asarray(linear, dtype=np.float64)
    return np.einsum("ij,...jk->...ik", m, rotations)
