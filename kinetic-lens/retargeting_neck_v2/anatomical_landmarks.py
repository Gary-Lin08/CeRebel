"""SOMA pose → anatomical landmark estimates (not renamed SOMA joints)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .config import OFFSETS_PATH, SOMA77_INDEX
from .rotations import rotation_from_y_z
from .soma_frames import SomaSequence, relative_rotation


LANDMARK_NAMES = (
    "C7",
    "T1",
    "JUGULAR",
    "L_ACROMION",
    "R_ACROMION",
    "HEAD_A",
    "HEAD_B",
    "HEAD_C",
)


def default_offsets() -> dict:
    """Offsets in SOMA body frames (metres). Chest/head frames: +Y superior, +Z right, +X forward."""
    return {
        "description": (
            "Body-frame offsets. Landmark = p_body + R_body @ delta. "
            "These are not optical markers; they are geometric estimates."
        ),
        "body_frames": {
            "C7": "Chest",
            "T1": "Chest",
            "JUGULAR": "Chest",
            "L_ACROMION": "Chest",
            "R_ACROMION": "Chest",
            "HEAD_A": "Head",
            "HEAD_B": "Head",
            "HEAD_C": "Head",
        },
        "delta_m": {
            "C7": [0.02, 0.11, 0.0],
            "T1": [-0.03, 0.08, 0.0],
            "JUGULAR": [0.08, 0.06, 0.0],
            "L_ACROMION": [0.02, 0.12, -0.16],
            "R_ACROMION": [0.02, 0.12, 0.16],
            "HEAD_A": [0.07, 0.11, 0.0],
            "HEAD_B": [0.04, 0.05, 0.06],
            "HEAD_C": [0.04, 0.05, -0.06],
        },
        "hierarchy_choice": "Chest→Neck1 and Neck2→Head",
    }


def load_or_init_offsets(path: Path | None = None) -> dict:
    path = path or OFFSETS_PATH
    if path.is_file():
        return json.loads(path.read_text())
    data = default_offsets()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data


def _body_rotations_from_joints(joints: np.ndarray) -> dict[str, np.ndarray]:
    """Fallback frames if FK rotations are missing; eyes provide head yaw."""
    i = SOMA77_INDEX
    chest_y = joints[:, i["Neck1"]] - joints[:, i["Chest"]]
    neck_y = joints[:, i["Head"]] - joints[:, i["Neck1"]]
    head_y = joints[:, i["HeadEnd"]] - joints[:, i["Head"]]
    right = joints[:, i["RightArm"]] - joints[:, i["LeftArm"]]
    eye_right = joints[:, i["RightEye"]] - joints[:, i["LeftEye"]]
    t = joints.shape[0]
    chest = np.stack([rotation_from_y_z(chest_y[k], right[k]) for k in range(t)])
    neck = np.stack([rotation_from_y_z(neck_y[k], right[k]) for k in range(t)])
    head = np.stack([rotation_from_y_z(head_y[k], eye_right[k]) for k in range(t)])
    return {"Chest": chest, "Neck1": neck, "Neck2": neck, "Head": head}


def soma_body_rotations(seq: SomaSequence) -> dict[str, np.ndarray]:
    names = ("Chest", "Neck1", "Neck2", "Head")
    out = {n: seq.rotations_opensim[:, SOMA77_INDEX[n]] for n in names}
    # Blend a geometric head frame so collinear spine joints cannot erase yaw.
    geom = _body_rotations_from_joints(seq.joints_opensim)
    out["Head"] = geom["Head"]
    return out


def evaluate_hierarchy_candidates(seq: SomaSequence) -> dict:
    pairs = [
        ("Chest", "Neck1"),
        ("Chest", "Neck2"),
        ("Neck1", "Head"),
        ("Neck2", "Head"),
    ]
    report = {}
    for parent, child in pairs:
        rel = relative_rotation(seq, parent, child)
        geodesics = []
        for t in range(1, rel.shape[0]):
            from .rotations import geodesic_deg

            geodesics.append(geodesic_deg(rel[t - 1], rel[t]))
        arr = np.array(geodesics) if geodesics else np.zeros(1)
        report[f"{parent}->{child}"] = {
            "mean_step_deg": round(float(np.mean(arr)), 4),
            "p95_step_deg": round(float(np.percentile(arr, 95)), 4),
            "max_step_deg": round(float(np.max(arr)), 4),
        }
    report["selected"] = "Chest→Neck1 (lower cervical), Neck2→Head (upper cervical / skull)"
    report["reason"] = (
        "Neck1 is the first cervical-like child of Chest; Head is parented to Neck2 in SOMA77. "
        "Using Neck2→Head keeps the C1/skull joint from absorbing the whole cervical chain."
    )
    return report


def landmark_trajectories(seq: SomaSequence, offsets: dict | None = None) -> dict[str, np.ndarray]:
    offsets = offsets or load_or_init_offsets()
    joints = seq.joints_opensim
    rotations = soma_body_rotations(seq)
    delta = offsets["delta_m"]
    bodies = offsets["body_frames"]
    out: dict[str, np.ndarray] = {}
    for name in LANDMARK_NAMES:
        body = bodies[name]
        d = np.asarray(delta[name], dtype=np.float64)
        p = joints[:, SOMA77_INDEX[body]]
        r = rotations[body]
        out[name] = p + np.einsum("tij,j->ti", r, d)
    # Acromion should stay near GH centres; blend with observed arm joints.
    out["L_ACROMION"] = 0.65 * out["L_ACROMION"] + 0.35 * joints[:, SOMA77_INDEX["LeftArm"]]
    out["R_ACROMION"] = 0.65 * out["R_ACROMION"] + 0.35 * joints[:, SOMA77_INDEX["RightArm"]]
    return out


def head_position(seq: SomaSequence) -> np.ndarray:
    return seq.joints_opensim[:, SOMA77_INDEX["Head"]]
