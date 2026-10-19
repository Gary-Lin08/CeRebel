"""Canonical MotionSequence loaded from a CeRebel session artifact set."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from motion_agent.motion.angles import (
    SOMERSAULT_CONVENTION,
    continuous_euler_xyz_from_quaternions,
    cumulative_rotation_from_quaternions,
    intrinsic_somersault_from_root,
    unwrap_radians,
)
from motion_agent.motion.smplx_mapping import (
    DYNAMICS_JOINT_TO_SMPL_X,
    OPENSIM_BODY_TO_SMPL_X,
    PRIMARY_ANGLE_COORD,
    resolve_joint_name,
)
from motion_agent.motion.synchronization import SyncMeta, frame_to_timestamp

LOG = logging.getLogger("motion_agent.sequence")

COORDINATE_SYSTEM = "OpenSim ground (Y-up, X-forward, Z-right), metres / radians"
UNITS = {
    "position": "m",
    "velocity": "m/s",
    "acceleration": "m/s^2",
    "angle": "rad",
    "angle_deg": "deg",
    "angular_velocity": "rad/s",
    "moment": "N·m",
    "force": "N",
    "power": "W",
}


@dataclass
class MotionSequence:
    video_id: str
    video_path: Path | None
    jpeg_dir: Path | None
    fps: float
    duration_s: float
    frame_count: int
    timestamps: np.ndarray  # (T,)
    # OpenSim primary angles keyed by SMPL-X name → (T,) continuous (unwrapped)
    joint_angles_rad: dict[str, np.ndarray]
    # Principal-value OpenSim DOF (may stall/reverse near ±π)
    joint_angles_rad_raw: dict[str, np.ndarray]
    # Body origin positions keyed by SMPL-X name → (T, 3)
    joint_positions: dict[str, np.ndarray]
    root_position: np.ndarray  # (T, 3) pelvis
    root_rotation_quat: np.ndarray  # (T, 4) OpenSim xyzw
    # Continuous root orientation from pelvis quaternion (OpenSim WORLD / ground)
    root_euler_xyz_rad: np.ndarray  # (T, 3) unwrapped XYZ Euler in world
    root_cumulative_rotation_rad: np.ndarray  # (T,) cumulative |ΔR| from t=0
    # Rider-intrinsic somersault (about rightward axis = facing × up)
    rider_facing_horizontal: np.ndarray  # (T, 3) unit vectors in world
    rider_lateral_horizontal: np.ndarray  # (T, 3) rightward unit vectors
    intrinsic_somersault_rate_rad_s: np.ndarray  # (T,) +backflip / −frontflip
    intrinsic_somersault_cumulative_rad: np.ndarray  # (T,) from t=0
    com_position: np.ndarray  # (T, 3)
    contact_left: np.ndarray  # (T,) bool
    contact_right: np.ndarray  # (T,) bool
    contact_phase: list[str]
    # Dynamics keyed by SMPL-X name
    joint_flexion_moment_nm: dict[str, np.ndarray]
    joint_angular_velocity_rad_s: dict[str, np.ndarray]
    joint_reaction_force_n: dict[str, np.ndarray]
    joint_power_w: dict[str, np.ndarray]
    grf_total_n: np.ndarray  # (T, 3)
    grf_left_n: np.ndarray
    grf_right_n: np.ndarray
    grf_left_active: np.ndarray
    grf_right_active: np.ndarray
    confidence: dict[str, np.ndarray]
    metadata: dict[str, Any] = field(default_factory=dict)
    opensim_coordinates: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def sync(self) -> SyncMeta:
        return SyncMeta(
            fps=self.fps,
            frame_count=self.frame_count,
            duration_s=self.duration_s,
            coordinate_system=COORDINATE_SYSTEM,
        )

    def has_dynamics(self) -> bool:
        return bool(self.joint_flexion_moment_nm)

    def has_video(self) -> bool:
        return bool(self.video_path and self.video_path.is_file()) or bool(
            self.jpeg_dir and self.jpeg_dir.is_dir()
        )


def _finite_diff(series: np.ndarray, dt: float) -> np.ndarray:
    """Central difference along axis 0; series (T,) or (T, D)."""
    if series.shape[0] < 2 or dt <= 0:
        return np.zeros_like(series)
    out = np.gradient(series, dt, axis=0)
    return out


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_session_paths(session_dir: Path) -> dict[str, Path | None]:
    session_dir = session_dir.resolve()
    ik_json = session_dir / "ik.json"
    paths: dict[str, Path | None] = {
        "session_dir": session_dir,
        "ik_json": ik_json if ik_json.is_file() else None,
        "opensim_motion": session_dir / "ik" / "opensim-motion.json",
        "estimated_dynamics": session_dir / "ik" / "estimated-dynamics.json",
        "pose3d": session_dir / "ik" / "pose3d.json",
        "video": session_dir / "ik" / "session.mp4",
        "jpeg_dir": session_dir / "jpegs",
        "header": session_dir / "header.json",
    }
    if ik_json.is_file():
        manifest = _load_json(ik_json)
        for key in ("opensim_motion", "estimated_dynamics", "pose3d", "video"):
            val = manifest.get(key)
            if val:
                p = Path(val)
                if p.is_file():
                    paths[key] = p
    return paths


def load_motion_sequence(session_dir: Path | str) -> MotionSequence:
    """Build MotionSequence from a finished CeRebel session directory."""
    session_dir = Path(session_dir)
    paths = load_session_paths(session_dir)
    osim_path = paths["opensim_motion"]
    if osim_path is None or not Path(osim_path).is_file():
        raise FileNotFoundError(f"opensim-motion.json missing under {session_dir}")

    osim = _load_json(Path(osim_path))
    frames = osim["frames"]
    n = len(frames)
    if n == 0:
        raise ValueError(f"empty opensim motion: {osim_path}")

    # FPS from ik.json / header / pose3d / inferred
    fps = 30.0
    ik_json = paths.get("ik_json")
    if ik_json and Path(ik_json).is_file():
        fps = float(_load_json(Path(ik_json)).get("fps") or fps)
    elif paths.get("header") and Path(paths["header"]).is_file():  # type: ignore[arg-type]
        fps = float(_load_json(Path(paths["header"])).get("target_fps") or fps)  # type: ignore[arg-type]

    timestamps = np.array([float(f["time"]) for f in frames], dtype=np.float64)
    # Prefer explicit times; fall back to uniform grid if degenerate
    if np.allclose(timestamps, timestamps[0]):
        timestamps = np.arange(n, dtype=np.float64) / fps
    duration_s = float(timestamps[-1]) if n == 1 else float(timestamps[-1])
    # Duration should cover last frame interval for video tools
    if n > 1:
        dt = float(np.median(np.diff(timestamps)))
        duration_s = float(timestamps[-1] + dt)
    else:
        dt = 1.0 / fps
        duration_s = 1.0 / fps

    # Collect all coordinate series (principal values), then unwrap for continuity.
    coord_names = list(frames[0].get("coordinates", {}).keys())
    opensim_coordinates_raw: dict[str, np.ndarray] = {
        name: np.array([float(f["coordinates"].get(name, 0.0)) for f in frames], dtype=np.float64)
        for name in coord_names
    }
    opensim_coordinates: dict[str, np.ndarray] = {
        name: unwrap_radians(series) for name, series in opensim_coordinates_raw.items()
    }

    joint_angles_rad_raw: dict[str, np.ndarray] = {}
    joint_angles_rad: dict[str, np.ndarray] = {}
    for joint, coord in PRIMARY_ANGLE_COORD.items():
        if coord in opensim_coordinates_raw:
            joint_angles_rad_raw[joint] = opensim_coordinates_raw[coord]
            joint_angles_rad[joint] = opensim_coordinates[coord]

    # Body positions
    joint_positions: dict[str, np.ndarray] = {}
    for body, smplx in OPENSIM_BODY_TO_SMPL_X.items():
        arr = np.zeros((n, 3), dtype=np.float64)
        present = False
        for i, f in enumerate(frames):
            bodies = f.get("bodies") or {}
            if body in bodies and "p" in bodies[body]:
                arr[i] = bodies[body]["p"]
                present = True
        if present:
            # Prefer first mapped body if multiple map to same joint
            if smplx not in joint_positions:
                joint_positions[smplx] = arr

    root_position = np.array(
        [f.get("pelvis") or (f.get("bodies") or {}).get("pelvis", {}).get("p", [0, 0, 0]) for f in frames],
        dtype=np.float64,
    )
    if root_position.ndim != 2:
        root_position = joint_positions.get("pelvis", np.zeros((n, 3)))

    root_rotation_quat = np.zeros((n, 4), dtype=np.float64)
    for i, f in enumerate(frames):
        q = (f.get("bodies") or {}).get("pelvis", {}).get("q")
        if q is not None:
            root_rotation_quat[i] = q  # OpenSim xyzw

    root_euler_xyz_rad = continuous_euler_xyz_from_quaternions(root_rotation_quat)
    root_cumulative_rotation_rad = cumulative_rotation_from_quaternions(root_rotation_quat)

    com_position = np.array([f.get("centerOfMass", [0.0, 0.0, 0.0]) for f in frames], dtype=np.float64)
    soma = intrinsic_somersault_from_root(root_rotation_quat, com_position, dt)
    rider_facing_horizontal = soma["facing_horizontal"]
    rider_lateral_horizontal = soma["lateral_horizontal"]
    intrinsic_somersault_rate_rad_s = soma["somersault_rate_rad_s"]
    intrinsic_somersault_cumulative_rad = soma["somersault_cumulative_rad"]

    # Dynamics (optional)
    contact_left = np.zeros(n, dtype=bool)
    contact_right = np.zeros(n, dtype=bool)
    contact_phase: list[str] = [""] * n
    joint_flexion_moment_nm: dict[str, np.ndarray] = {}
    joint_angular_velocity_rad_s: dict[str, np.ndarray] = {}
    joint_reaction_force_n: dict[str, np.ndarray] = {}
    joint_power_w: dict[str, np.ndarray] = {}
    confidence: dict[str, np.ndarray] = {}
    grf_total_n = np.zeros((n, 3), dtype=np.float64)
    grf_left_n = np.zeros((n, 3), dtype=np.float64)
    grf_right_n = np.zeros((n, 3), dtype=np.float64)
    grf_left_active = np.zeros(n, dtype=bool)
    grf_right_active = np.zeros(n, dtype=bool)
    dyn_meta: dict[str, Any] = {}

    dyn_path = paths.get("estimated_dynamics")
    if dyn_path and Path(dyn_path).is_file():
        dyn = _load_json(Path(dyn_path))
        dyn_meta = dyn.get("metadata") or {}
        dyn_frames = dyn.get("frames") or []
        # Index dynamics by nearest opensim timestamp if lengths differ
        dyn_t = np.array([float(f["time"]) for f in dyn_frames], dtype=np.float64) if dyn_frames else timestamps
        for smplx_name, dyn_id in ((v, k) for k, v in DYNAMICS_JOINT_TO_SMPL_X.items()):
            joint_flexion_moment_nm[smplx_name] = np.zeros(n, dtype=np.float64)
            joint_angular_velocity_rad_s[smplx_name] = np.zeros(n, dtype=np.float64)
            joint_reaction_force_n[smplx_name] = np.zeros((n, 3), dtype=np.float64)
            confidence[smplx_name] = np.zeros(n, dtype=np.float64)

        for i, t in enumerate(timestamps):
            j = int(np.argmin(np.abs(dyn_t - t))) if len(dyn_t) else 0
            if j >= len(dyn_frames):
                continue
            df = dyn_frames[j]
            contact = df.get("contact") or {}
            contact_left[i] = bool(contact.get("left", False))
            contact_right[i] = bool(contact.get("right", False))
            contact_phase[i] = str(contact.get("phase") or "")
            grf = df.get("grf") or {}
            total = (grf.get("total") or {}).get("force")
            if total is not None:
                grf_total_n[i] = total
            left = grf.get("left") or {}
            right = grf.get("right") or {}
            if left.get("force") is not None:
                grf_left_n[i] = left["force"]
            if right.get("force") is not None:
                grf_right_n[i] = right["force"]
            grf_left_active[i] = bool(left.get("active", False))
            grf_right_active[i] = bool(right.get("active", False))
            joints = df.get("joints") or {}
            for dyn_id, smplx_name in DYNAMICS_JOINT_TO_SMPL_X.items():
                jj = joints.get(dyn_id) or {}
                joint_flexion_moment_nm[smplx_name][i] = float(jj.get("flexionMomentNm") or 0.0)
                joint_angular_velocity_rad_s[smplx_name][i] = float(
                    jj.get("angularVelocityRadS") or 0.0
                )
                force = jj.get("force")
                if force is not None:
                    joint_reaction_force_n[smplx_name][i] = force
                confidence[smplx_name][i] = float(jj.get("confidence") or 0.0)

        # Mechanical power: P = tau * omega (flexion moment × flexion angular velocity)
        for smplx_name in joint_flexion_moment_nm:
            joint_power_w[smplx_name] = (
                joint_flexion_moment_nm[smplx_name] * joint_angular_velocity_rad_s[smplx_name]
            )

    video_path = Path(paths["video"]) if paths.get("video") else None
    if video_path and not video_path.is_file():
        video_path = None
    jpeg_dir = Path(paths["jpeg_dir"]) if paths.get("jpeg_dir") else None
    if jpeg_dir and not jpeg_dir.is_dir():
        jpeg_dir = None

    osim_meta = osim.get("metadata") or {}
    seq = MotionSequence(
        video_id=session_dir.name,
        video_path=video_path,
        jpeg_dir=jpeg_dir,
        fps=float(fps),
        duration_s=duration_s,
        frame_count=n,
        timestamps=timestamps,
        joint_angles_rad=joint_angles_rad,
        joint_angles_rad_raw=joint_angles_rad_raw,
        joint_positions=joint_positions,
        root_position=root_position,
        root_rotation_quat=root_rotation_quat,
        root_euler_xyz_rad=root_euler_xyz_rad,
        root_cumulative_rotation_rad=root_cumulative_rotation_rad,
        rider_facing_horizontal=rider_facing_horizontal,
        rider_lateral_horizontal=rider_lateral_horizontal,
        intrinsic_somersault_rate_rad_s=intrinsic_somersault_rate_rad_s,
        intrinsic_somersault_cumulative_rad=intrinsic_somersault_cumulative_rad,
        com_position=com_position,
        contact_left=contact_left,
        contact_right=contact_right,
        contact_phase=contact_phase,
        joint_flexion_moment_nm=joint_flexion_moment_nm,
        joint_angular_velocity_rad_s=joint_angular_velocity_rad_s,
        joint_reaction_force_n=joint_reaction_force_n,
        joint_power_w=joint_power_w,
        grf_total_n=grf_total_n,
        grf_left_n=grf_left_n,
        grf_right_n=grf_right_n,
        grf_left_active=grf_left_active,
        grf_right_active=grf_right_active,
        confidence=confidence,
        metadata={
            "coordinate_system": COORDINATE_SYSTEM,
            "units": UNITS,
            "source_estimator": "GEM-X SOMA77 → OpenSim Rajagopal IK",
            "angle_policy": (
                "OpenSim DOF series are np.unwrap'ed for continuity. "
                "Root Euler / cumulative rotation are OpenSim WORLD (Y-up) — use them "
                "for flip extent, not front vs back. For frontflip vs backflip use "
                "rider-intrinsic somersault (+backflip / −frontflip). "
                "DOF pelvis_tilt can stall/reverse near ±180°."
            ),
            "somersault_convention": SOMERSAULT_CONVENTION,
            "opensim_model": osim_meta.get("modelName"),
            "opensim_warning": osim_meta.get("warning"),
            "dynamics_metadata": dyn_meta,
            "subject_height_cm": osim_meta.get("subjectHeightCm"),
            "subject_mass_kg": osim_meta.get("subjectMassKg"),
            "dt": dt,
            "paths": {k: str(v) if v else None for k, v in paths.items()},
        },
        opensim_coordinates=opensim_coordinates,
    )
    LOG.info(
        "loaded MotionSequence session=%s frames=%s duration=%.2fs dynamics=%s",
        seq.video_id,
        seq.frame_count,
        seq.duration_s,
        seq.has_dynamics(),
    )
    return seq


def joint_angle_deg(seq: MotionSequence, joint: str) -> np.ndarray:
    name = resolve_joint_name(joint)
    if name not in seq.joint_angles_rad:
        raise KeyError(f"No primary angle series for joint {name}")
    return np.degrees(seq.joint_angles_rad[name])


def velocity_of(position: np.ndarray, dt: float) -> np.ndarray:
    return _finite_diff(position, dt)


def acceleration_of(position: np.ndarray, dt: float) -> np.ndarray:
    return _finite_diff(_finite_diff(position, dt), dt)
