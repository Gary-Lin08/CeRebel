"""SOMA body frame → SOMA anatomical frame → OpenSim anatomical frame."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .anatomical_landmarks import soma_body_rotations
from .config import HEAD_IN_NECK, NECK_IN_TORSO, SOMA_TO_OPENSIM_CALIB, VISUAL_SCALE
from .rotations import orthonormalize
from .soma_frames import SomaSequence


def _neutral_index(seq: SomaSequence) -> int:
    """Pick a near-upright frame (max head height, moderate limb span)."""
    head_y = seq.joints_opensim[:, 6, 1]
    return int(np.argmax(head_y))


def calibrate(seq: SomaSequence, path: Path | None = None) -> dict:
    """Fixed per-body calibration rotations from a static/upright frame.

    OpenSim default pose has identity body orientations in ground (Rajagopal
    default coordinates are zero). SOMA body frames are constructed/FK'd in the
    already yaw-normalized OpenSim world. The calibration is therefore
    R_calib = R_soma_neutral^T  so that R_soma @ R_calib ≈ I at rest.
    """
    t0 = _neutral_index(seq)
    bodies = soma_body_rotations(seq)
    calib = {}
    for name, mats in bodies.items():
        r0 = orthonormalize(mats[t0])
        calib[name] = r0.T.tolist()
    payload = {
        "neutral_index": t0,
        "world": {
            "soma_after_yaw_normalize": "+X right, +Y up, +Z forward",
            "opensim": "+X forward, +Y up, +Z right",
            "map": "P = [[0,0,1],[0,1,0],[1,0,0]] applied in soma_frames.align_joints_and_rotations",
        },
        "R_soma_body_to_opensim_anatomy": calib,
        "neck_in_torso_m_unscaled": list(NECK_IN_TORSO),
        "head_in_neck_m_unscaled": list(HEAD_IN_NECK),
        "visual_scale": VISUAL_SCALE,
        "note": (
            "Yaw removal is the production per-frame hip facing basis; this file does not "
            "introduce a second world yaw convention."
        ),
    }
    dest = path or SOMA_TO_OPENSIM_CALIB
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def apply_body_calibration(rotations: np.ndarray, r_calib_list: list) -> np.ndarray:
    r_c = np.asarray(r_calib_list, dtype=np.float64)
    return rotations @ r_c
