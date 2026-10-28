"""Load SOMA77 joints + local axis-angle poses without mutating production converters."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import (
    GEMX_PYTHON,
    GEMX_ROOT,
    PACKAGE_DIR,
    SOMA77_INDEX,
    SOMA77_PARENTS,
    SOMA_ASSETS,
)
from .rotations import (
    P_SOMA_YUP_TO_OPENSIM,
    apply_world_linear_map,
    batch_rodrigues,
    forward_kinematics_rotations,
)


@dataclass
class SomaSequence:
    joints_opensim: np.ndarray  # (T, 77, 3)
    rotations_opensim: np.ndarray  # (T, 77, 3, 3)
    times: np.ndarray
    fps: float
    global_yaw_rad: np.ndarray
    source: str
    session_id: str


def _production_sys_path() -> None:
    root = str(PACKAGE_DIR.parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)


def align_joints_and_rotations(
    joints_raw: np.ndarray,
    local_axis_angles: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Apply production yaw-normalize + SOMA→OpenSim map to joints and FK rotations."""
    _production_sys_path()
    from cerebel_ik_retarget import global_yaw_series, normalize_soma77_joints, soma_yup_to_opensim

    yaw = global_yaw_series(joints_raw)
    joints_yup = normalize_soma77_joints(joints_raw)
    joints_osim = np.stack([soma_yup_to_opensim(frame) for frame in joints_yup], axis=0)

    local_r = batch_rodrigues(local_axis_angles)
    world_raw = forward_kinematics_rotations(local_r, SOMA77_PARENTS)

    aligned_r = np.empty_like(world_raw)
    for t in range(joints_raw.shape[0]):
        right, forward = _facing_basis(joints_raw[t])
        m = np.stack([right, np.array([0.0, 1.0, 0.0]), forward], axis=0)
        aligned_r[t] = np.einsum("ij,njk->nik", m, world_raw[t])
    rotations_osim = apply_world_linear_map(aligned_r, P_SOMA_YUP_TO_OPENSIM)
    return joints_osim, rotations_osim, yaw


def _facing_basis(ref: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    _production_sys_path()
    from cerebel_ik_retarget import _facing_basis as production_facing

    return production_facing(ref)


def load_npz(path: Path) -> SomaSequence:
    data = np.load(path, allow_pickle=False)

    def _text(key: str) -> str:
        val = data[key]
        if val.shape == ():
            return str(val)
        return str(val.reshape(-1)[0])

    def _scalar(key: str) -> float:
        return float(np.asarray(data[key]).reshape(-1)[0])

    return SomaSequence(
        joints_opensim=data["joints_opensim"],
        rotations_opensim=data["rotations_opensim"],
        times=data["times"],
        fps=_scalar("fps"),
        global_yaw_rad=data["global_yaw_rad"],
        source=_text("source"),
        session_id=_text("session_id"),
    )


def save_npz(path: Path, seq: SomaSequence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        joints_opensim=seq.joints_opensim,
        rotations_opensim=seq.rotations_opensim,
        times=seq.times,
        fps=np.array([seq.fps]),
        global_yaw_rad=seq.global_yaw_rad,
        source=np.array([seq.source]),
        session_id=np.array([seq.session_id]),
    )


def relative_rotation(seq: SomaSequence, parent: str, child: str) -> np.ndarray:
    pi, ci = SOMA77_INDEX[parent], SOMA77_INDEX[child]
    rp = seq.rotations_opensim[:, pi]
    rc = seq.rotations_opensim[:, ci]
    return np.einsum("tij,tjk->tik", np.transpose(rp, (0, 2, 1)), rc)


def session_paths(session_id: str) -> dict[str, Path]:
    import os

    from .config import GEMX_ROOT, SESSIONS_DIR

    session = SESSIONS_DIR / session_id
    pose3d = session / "ik" / "pose3d.json"
    gemx = session / "gemx.json"
    hpe = None
    fps = 30.0
    # Session-local symlink written by the IK pipeline (survives SESSIONS_DIR mismatches).
    linked = session / "ik" / "hpe_results.pt"
    if linked.is_file() or linked.is_symlink():
        try:
            resolved = linked.resolve(strict=True)
            if resolved.is_file():
                hpe = resolved
        except FileNotFoundError:
            pass
    if pose3d.is_file():
        meta = json.loads(pose3d.read_text())["metadata"]
        fps = float(meta["fps"])
        src = meta.get("source")
        if hpe is None and src and Path(src).is_file():
            hpe = Path(src)
    if hpe is None and gemx.is_file():
        g = json.loads(gemx.read_text())
        cand = Path(g.get("hpe_results", ""))
        if cand.is_file():
            hpe = cand
        fps = float(g.get("fps", fps))
    if hpe is None:
        # Last resort: canonical GEM-X output layout, independent of sessions dir.
        gemx_root = Path(os.environ.get("CEREBEL_GEMX_OUTPUT_ROOT", str(GEMX_ROOT / "outputs" / "cerebel")))
        cand = gemx_root / session_id / "hpe_results.pt"
        if cand.is_file():
            hpe = cand
    return {"session": session, "pose3d": pose3d, "hpe": hpe, "fps": fps}  # type: ignore[dict-item]


def extract_worker_payload() -> str:
    """Path of the GEM-X worker script (runs under GEMX_PYTHON)."""
    return str(PACKAGE_DIR / "_extract_worker.py")
