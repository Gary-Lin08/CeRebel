"""Synthetic 3D body motions for sport-detection tests (OpenSim frame: Y up, metres).

Only the joints the detectors read are filled in: wrists, shoulders, hips, ankles
and pelvis. The body stands at the origin, facing +X, shoulders 0.36 m wide and
a 0.5 m torso (hip centres 0.92 m, shoulders 1.42 m up).
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

FPS = 30.0
HIP_Y = 0.92
SHOULDER_Y = 1.42


def _smooth(s: np.ndarray) -> np.ndarray:
    s = np.clip(s, 0.0, 1.0)
    return s * s * (3.0 - 2.0 * s)


def _ease_in(s: np.ndarray) -> np.ndarray:
    s = np.clip(s, 0.0, 1.0)
    return s * s


def _ease_out(s: np.ndarray) -> np.ndarray:
    s = np.clip(s, 0.0, 1.0)
    return 1.0 - (1.0 - s) ** 2


def _path(t: np.ndarray, keys: list[tuple[float, tuple[float, float, float], str]]) -> np.ndarray:
    """Piecewise path: hold the first point, then move to each (time, point) with an easing."""
    ease = {"smooth": _smooth, "in": _ease_in, "out": _ease_out}
    out = np.repeat(np.asarray(keys[0][1], dtype=float)[None, :], t.size, axis=0)
    for (t0, p0, _), (t1, p1, how) in zip(keys, keys[1:]):
        w = ease[how]((t - t0) / max(t1 - t0, 1e-6))[:, None]
        moving = t >= t0
        out[moving] = (np.asarray(p0) + w * (np.asarray(p1) - np.asarray(p0)))[moving]
    return out


def _standing(t: np.ndarray) -> dict[str, np.ndarray]:
    def fixed(x: float, y: float, z: float) -> np.ndarray:
        return np.repeat(np.array([[x, y, z]], dtype=float), t.size, axis=0)

    return {
        "pelvis": fixed(0.0, 0.95, 0.0),
        "left_hip": fixed(0.0, HIP_Y, -0.09),
        "right_hip": fixed(0.0, HIP_Y, 0.09),
        "left_shoulder": fixed(0.0, SHOULDER_Y, -0.18),
        "right_shoulder": fixed(0.0, SHOULDER_Y, 0.18),
        "left_ankle": fixed(0.0, 0.08, -0.15),
        "right_ankle": fixed(0.0, 0.08, 0.15),
    }


def as_sequence(t: np.ndarray, joints: dict[str, np.ndarray]) -> SimpleNamespace:
    dt = float(t[1] - t[0])
    return SimpleNamespace(
        video_id="synthetic",
        fps=FPS,
        timestamps=t,
        duration_s=float(t[-1] + dt),
        frame_count=int(t.size),
        joint_positions=joints,
        root_position=joints["pelvis"],
        metadata={"dt": dt},
    )


def _clock(duration_s: float) -> np.ndarray:
    return np.arange(int(round(duration_s * FPS))) / FPS


def golf_swing(
    *,
    duration_s: float = 3.0,
    time_scale: float = 1.0,
    release_lead_hand: bool = False,
    jump_m: float = 0.0,
    slide_m: float = 0.0,
    address_lift_m: float = 0.0,
    impact_lift_m: float = 0.0,
) -> SimpleNamespace:
    """Right-handed full swing: still address, backswing, fast downswing, finish.

    The keyword switches each break exactly one golf trait, so a test can check
    that one rule — and only that rule — catches it.
    """
    t = _clock(duration_s)
    k = time_scale
    t_take, t_top, t_imp, t_fin = 0.8 * k, 1.7 * k, 2.0 * k, 2.5 * k
    theta = np.zeros_like(t)
    back = (t >= t_take) & (t < t_top)
    theta[back] = 0.6 * np.pi * _smooth((t[back] - t_take) / (t_top - t_take))
    down = (t >= t_top) & (t < t_imp)
    theta[down] = 0.6 * np.pi * (1.0 - ((t[down] - t_top) / (t_imp - t_top)) ** 2)
    follow = t >= t_imp
    theta[follow] = -0.7 * np.pi * _ease_out((t[follow] - t_imp) / (t_fin - t_imp))

    # Hands ride a 0.6 m circle in a tilted plane: bottom of the arc in front of
    # the thighs, top up behind the trail shoulder.
    centre = np.array([0.15, 1.35, 0.0])
    down_dir = np.array([0.406, -0.914, 0.0])
    side_dir = np.array([0.0, 0.0, 1.0])
    hands = centre + 0.6 * (np.cos(theta)[:, None] * down_dir + np.sin(theta)[:, None] * side_dir)

    lift = np.zeros_like(t)
    if address_lift_m:
        lift += address_lift_m * (1.0 - _smooth((t - t_take) / (0.3 * k)))
    if impact_lift_m:
        near = np.abs(t - t_imp) < 0.25 * k
        lift[near] += impact_lift_m * np.cos(np.pi * (t[near] - t_imp) / (0.5 * k)) ** 2
    hands[:, 1] += lift

    joints = _standing(t)
    joints["left_wrist"] = hands + np.array([0.0, 0.0, -0.05])
    joints["right_wrist"] = hands + np.array([0.0, 0.0, 0.05])
    if release_lead_hand:
        away = _path(t, [(t_take, tuple(joints["left_wrist"][0]), "smooth"),
                         (t_take + 0.4 * k, (0.3, 1.1, -0.55), "smooth")])
        joints["left_wrist"] = np.where((t >= t_take)[:, None], away, joints["left_wrist"])

    shift = np.zeros((t.size, 3))
    if jump_m:
        up = (t >= t_top) & (t <= t_imp + 0.1 * k)
        shift[up, 1] = jump_m * np.sin(np.pi * (t[up] - t_top) / (t_imp + 0.1 * k - t_top))
    if slide_m:
        shift[:, 0] = slide_m * _smooth((t - t_take) / (t_imp - t_take))
    for name in joints:
        joints[name] = joints[name] + shift
    return as_sequence(t, joints)


def tennis_forehand() -> SimpleNamespace:
    """One-handed forehand: hands part at the unit turn, racket hand whips through waist height."""
    t = _clock(3.0)
    joints = _standing(t)
    joints["right_wrist"] = _path(t, [
        (0.8, (0.35, 1.0, 0.05), "smooth"),
        (1.4, (-0.25, 1.15, 0.55), "smooth"),
        (1.75, (0.55, 1.0, 0.25), "in"),
        (2.2, (0.15, 1.5, -0.45), "out"),
    ])
    joints["left_wrist"] = _path(t, [
        (0.8, (0.35, 1.0, -0.05), "smooth"),
        (1.4, (0.45, 1.25, 0.1), "smooth"),
        (1.75, (0.25, 1.15, -0.45), "smooth"),
    ])
    joints["left_ankle"] = _path(t, [(0.8, (0.0, 0.08, -0.15), "smooth"), (1.4, (0.35, 0.08, -0.15), "smooth")])
    return as_sequence(t, joints)


def tennis_serve() -> SimpleNamespace:
    """Toss with the left hand, racket hand meets the ball well above the head."""
    t = _clock(3.0)
    joints = _standing(t)
    joints["right_wrist"] = _path(t, [
        (0.6, (0.3, 1.05, 0.1), "smooth"),
        (1.3, (-0.15, 1.6, 0.35), "smooth"),
        (1.55, (-0.25, 1.25, 0.3), "smooth"),
        (1.85, (0.35, 2.2, 0.05), "in"),
        (2.3, (0.35, 0.85, -0.35), "out"),
    ])
    joints["left_wrist"] = _path(t, [
        (0.6, (0.3, 1.05, 0.0), "smooth"),
        (1.3, (0.35, 2.0, -0.05), "smooth"),
        (1.85, (0.2, 1.2, -0.3), "smooth"),
    ])
    return as_sequence(t, joints)


def basketball_layup() -> SimpleNamespace:
    """Run in pumping the arms, jump off one foot, reach the ball up to the rim."""
    t = _clock(3.0)
    joints = _standing(t)
    run_x = np.where(t < 1.6, 3.0 * t, 4.8 + 1.5 * (t - 1.6))
    run_x = np.where(t > 2.2, 5.7 + 0.5 * (t - 2.2), run_x)
    jump = np.where((t >= 1.6) & (t <= 2.2), 0.5 * np.sin(np.pi * (t - 1.6) / 0.6), 0.0)
    bob = np.where(t < 1.6, 0.03 * np.sin(2 * np.pi * 3.0 * t), 0.0)
    for name in joints:
        joints[name] = joints[name] + np.stack([run_x, jump + bob, np.zeros_like(t)], axis=1)
    stride = np.where(t < 1.6, 0.3 * np.sin(2 * np.pi * 1.5 * t), 0.0)
    joints["left_ankle"][:, 0] += stride
    joints["right_ankle"][:, 0] -= stride
    pump = np.where(t < 1.6, 0.25 * np.sin(2 * np.pi * 1.5 * t), 0.0)
    base_l = np.stack([run_x - pump, 1.0 + jump, np.full_like(t, -0.25)], axis=1)
    base_r = np.stack([run_x + pump, 1.0 + jump, np.full_like(t, 0.25)], axis=1)
    reach = _smooth((t - 1.6) / 0.3) - _smooth((t - 2.1) / 0.4)
    joints["right_wrist"] = base_r + reach[:, None] * np.array([0.3, 1.3, -0.05])
    joints["left_wrist"] = base_l + reach[:, None] * np.array([0.2, 0.6, 0.05])
    return as_sequence(t, joints)


def bat_swing() -> SimpleNamespace:
    """Right-handed batter: hands start by the rear shoulder, stride, level swing at belt-chest height."""
    t = _clock(3.0)
    joints = _standing(t)
    hands = _path(t, [
        (1.0, (-0.05, 1.45, 0.25), "smooth"),
        (1.35, (-0.15, 1.5, 0.35), "smooth"),
        (1.6, (0.35, 1.15, -0.05), "in"),
        (2.0, (0.1, 1.5, -0.45), "out"),
    ])
    joints["left_wrist"] = hands + np.array([0.0, -0.04, 0.0])
    joints["right_wrist"] = hands + np.array([0.0, 0.04, 0.0])
    joints["left_ankle"] = _path(t, [(1.0, (0.0, 0.08, -0.15), "smooth"), (1.35, (0.0, 0.08, -0.45), "smooth")])
    return as_sequence(t, joints)


def arm_wave() -> SimpleNamespace:
    """Standing, raising the right hand and waving at the camera — no swing at all."""
    t = _clock(3.0)
    joints = _standing(t)
    joints["left_wrist"] = np.repeat(np.array([[0.0, 0.85, -0.25]]), t.size, axis=0)
    raise_ = _path(t, [(0.0, (0.0, 0.85, 0.25), "smooth"), (1.0, (0.05, 1.6, 0.35), "smooth")])
    wave = np.stack([np.zeros_like(t), 0.1 * np.sin(4 * np.pi * t), 0.1 * np.cos(4 * np.pi * t)], axis=1)
    joints["right_wrist"] = raise_ + np.where((t > 1.0)[:, None], wave - wave[int(FPS)], 0.0)
    return as_sequence(t, joints)
