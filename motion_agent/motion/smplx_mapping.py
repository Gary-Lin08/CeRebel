"""Authoritative SMPL-X joint naming and cross-estimator aliases.

Pose-vector indices, joint indices, and mesh vertex indices are distinct spaces.
Never treat them as interchangeable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# Official SMPL-X body+face joint order (55 joints before hands in common dumps).
# Hand joints follow SMPL-X MANO chain naming used by the project brief.
SMPL_X_JOINT_NAMES: tuple[str, ...] = (
    "pelvis",
    "left_hip",
    "right_hip",
    "spine1",
    "left_knee",
    "right_knee",
    "spine2",
    "left_ankle",
    "right_ankle",
    "spine3",
    "left_foot",
    "right_foot",
    "neck",
    "left_collar",
    "right_collar",
    "head",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "jaw",
    "left_eye_smplhf",
    "right_eye_smplhf",
    # Left hand
    "left_index1",
    "left_index2",
    "left_index3",
    "left_middle1",
    "left_middle2",
    "left_middle3",
    "left_pinky1",
    "left_pinky2",
    "left_pinky3",
    "left_ring1",
    "left_ring2",
    "left_ring3",
    "left_thumb1",
    "left_thumb2",
    "left_thumb3",
    # Right hand
    "right_index1",
    "right_index2",
    "right_index3",
    "right_middle1",
    "right_middle2",
    "right_middle3",
    "right_pinky1",
    "right_pinky2",
    "right_pinky3",
    "right_ring1",
    "right_ring2",
    "right_ring3",
    "right_thumb1",
    "right_thumb2",
    "right_thumb3",
)

_NAME_TO_IDX: dict[str, int] = {n: i for i, n in enumerate(SMPL_X_JOINT_NAMES)}

# Parent joint name for hierarchy (None = root). Incomplete for unused face/hand
# leaves is fine; tools only need parents for joints we actually expose.
SMPL_X_PARENTS: dict[str, str | None] = {
    "pelvis": None,
    "left_hip": "pelvis",
    "right_hip": "pelvis",
    "spine1": "pelvis",
    "left_knee": "left_hip",
    "right_knee": "right_hip",
    "spine2": "spine1",
    "left_ankle": "left_knee",
    "right_ankle": "right_knee",
    "spine3": "spine2",
    "left_foot": "left_ankle",
    "right_foot": "right_ankle",
    "neck": "spine3",
    "left_collar": "spine3",
    "right_collar": "spine3",
    "head": "neck",
    "left_shoulder": "left_collar",
    "right_shoulder": "right_collar",
    "left_elbow": "left_shoulder",
    "right_elbow": "right_shoulder",
    "left_wrist": "left_elbow",
    "right_wrist": "right_elbow",
    "jaw": "head",
    "left_eye_smplhf": "head",
    "right_eye_smplhf": "head",
}

for _side in ("left", "right"):
    for _finger in ("index", "middle", "pinky", "ring", "thumb"):
        SMPL_X_PARENTS[f"{_side}_{_finger}1"] = f"{_side}_wrist"
        SMPL_X_PARENTS[f"{_side}_{_finger}2"] = f"{_side}_{_finger}1"
        SMPL_X_PARENTS[f"{_side}_{_finger}3"] = f"{_side}_{_finger}2"

# Semantic aliases accepted by tools → canonical SMPL-X name.
JOINT_ALIASES: dict[str, str] = {
    # OpenSim body / dynamics IDs
    "hip_r": "right_hip",
    "hip_l": "left_hip",
    "knee_r": "right_knee",
    "knee_l": "left_knee",
    "ankle_r": "right_ankle",
    "ankle_l": "left_ankle",
    "femur_r": "right_hip",
    "femur_l": "left_hip",
    "tibia_r": "right_knee",
    "tibia_l": "left_knee",
    "talus_r": "right_ankle",
    "talus_l": "left_ankle",
    "calcn_r": "right_foot",
    "calcn_l": "left_foot",
    "toes_r": "right_foot",
    "toes_l": "left_foot",
    "humerus_r": "right_shoulder",
    "humerus_l": "left_shoulder",
    "ulna_r": "right_elbow",
    "ulna_l": "left_elbow",
    "radius_r": "right_elbow",
    "radius_l": "left_elbow",
    "hand_r": "right_wrist",
    "hand_l": "left_wrist",
    "torso": "spine3",
    # SOMA-style short names
    "hips": "pelvis",
    "l_hip": "left_hip",
    "r_hip": "right_hip",
    "l_knee": "left_knee",
    "r_knee": "right_knee",
    "l_ankle": "left_ankle",
    "r_ankle": "right_ankle",
    "l_toe": "left_foot",
    "r_toe": "right_foot",
    "l_shoulder": "left_shoulder",
    "r_shoulder": "right_shoulder",
    "l_elbow": "left_elbow",
    "r_elbow": "right_elbow",
    "l_wrist": "left_wrist",
    "r_wrist": "right_wrist",
    "l_thumb": "left_thumb1",
    "r_thumb": "right_thumb1",
    "l_index": "left_index1",
    "r_index": "right_index1",
    "l_pinky": "left_pinky1",
    "r_pinky": "right_pinky1",
}

# OpenSim DOF coordinate → (smplx joint, anatomical quantity label, units)
OPENSIM_COORD_TO_JOINT: dict[str, tuple[str, str]] = {
    "hip_flexion_r": ("right_hip", "flexion_rad"),
    "hip_adduction_r": ("right_hip", "adduction_rad"),
    "hip_rotation_r": ("right_hip", "rotation_rad"),
    "hip_flexion_l": ("left_hip", "flexion_rad"),
    "hip_adduction_l": ("left_hip", "adduction_rad"),
    "hip_rotation_l": ("left_hip", "rotation_rad"),
    "knee_angle_r": ("right_knee", "flexion_rad"),
    "knee_angle_l": ("left_knee", "flexion_rad"),
    "ankle_angle_r": ("right_ankle", "flexion_rad"),
    "ankle_angle_l": ("left_ankle", "flexion_rad"),
    "arm_flex_r": ("right_shoulder", "flexion_rad"),
    "arm_add_r": ("right_shoulder", "adduction_rad"),
    "arm_rot_r": ("right_shoulder", "rotation_rad"),
    "arm_flex_l": ("left_shoulder", "flexion_rad"),
    "arm_add_l": ("left_shoulder", "adduction_rad"),
    "arm_rot_l": ("left_shoulder", "rotation_rad"),
    "elbow_flex_r": ("right_elbow", "flexion_rad"),
    "elbow_flex_l": ("left_elbow", "flexion_rad"),
    "wrist_flex_r": ("right_wrist", "flexion_rad"),
    "wrist_flex_l": ("left_wrist", "flexion_rad"),
    "lumbar_extension": ("spine3", "extension_rad"),
    "lumbar_bending": ("spine3", "bending_rad"),
    "lumbar_rotation": ("spine3", "rotation_rad"),
    "pelvis_tilt": ("pelvis", "tilt_rad"),
    "pelvis_list": ("pelvis", "list_rad"),
    "pelvis_rotation": ("pelvis", "rotation_rad"),
}

# Dynamics JSON jointIds → SMPL-X
DYNAMICS_JOINT_TO_SMPL_X: dict[str, str] = {
    "hip_r": "right_hip",
    "hip_l": "left_hip",
    "knee_r": "right_knee",
    "knee_l": "left_knee",
    "ankle_r": "right_ankle",
    "ankle_l": "left_ankle",
}

# Body origin in opensim-motion bodies{} → SMPL-X (for positions)
OPENSIM_BODY_TO_SMPL_X: dict[str, str] = {
    "pelvis": "pelvis",
    "femur_r": "right_hip",
    "tibia_r": "right_knee",
    "talus_r": "right_ankle",
    "calcn_r": "right_foot",
    "toes_r": "right_foot",
    "femur_l": "left_hip",
    "tibia_l": "left_knee",
    "talus_l": "left_ankle",
    "calcn_l": "left_foot",
    "toes_l": "left_foot",
    "torso": "spine3",
    "humerus_r": "right_shoulder",
    "ulna_r": "right_elbow",
    "radius_r": "right_elbow",
    "hand_r": "right_wrist",
    "humerus_l": "left_shoulder",
    "ulna_l": "left_elbow",
    "radius_l": "left_elbow",
    "hand_l": "left_wrist",
}

# Primary flexion/extension OpenSim coordinate per SMPL-X joint (for angles)
PRIMARY_ANGLE_COORD: dict[str, str] = {
    "pelvis": "pelvis_tilt",
    "left_hip": "hip_flexion_l",
    "right_hip": "hip_flexion_r",
    "left_knee": "knee_angle_l",
    "right_knee": "knee_angle_r",
    "left_ankle": "ankle_angle_l",
    "right_ankle": "ankle_angle_r",
    "left_shoulder": "arm_flex_l",
    "right_shoulder": "arm_flex_r",
    "left_elbow": "elbow_flex_l",
    "right_elbow": "elbow_flex_r",
    "left_wrist": "wrist_flex_l",
    "right_wrist": "wrist_flex_r",
    "spine3": "lumbar_extension",
}


@dataclass(frozen=True)
class JointMetadata:
    name: str
    joint_index: int
    parent: str | None
    opensim_bodies: tuple[str, ...]
    primary_angle_coord: str | None
    dynamics_id: str | None


def resolve_joint_name(name: str) -> str:
    """Normalize aliases to a canonical SMPL-X joint name."""
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    if key in _NAME_TO_IDX:
        return key
    if key in JOINT_ALIASES:
        return JOINT_ALIASES[key]
    raise KeyError(f"Unknown joint name: {name!r}")


def joint_idx(name: str) -> int:
    """Return SMPL-X joint index for a semantic name (not pose-vector index)."""
    return _NAME_TO_IDX[resolve_joint_name(name)]


def joint_name(index: int) -> str:
    if index < 0 or index >= len(SMPL_X_JOINT_NAMES):
        raise IndexError(f"Joint index out of range: {index}")
    return SMPL_X_JOINT_NAMES[index]


def joint_metadata(name: str) -> JointMetadata:
    canon = resolve_joint_name(name)
    bodies = tuple(b for b, j in OPENSIM_BODY_TO_SMPL_X.items() if j == canon)
    dyn = next((d for d, j in DYNAMICS_JOINT_TO_SMPL_X.items() if j == canon), None)
    return JointMetadata(
        name=canon,
        joint_index=_NAME_TO_IDX[canon],
        parent=SMPL_X_PARENTS.get(canon),
        opensim_bodies=bodies,
        primary_angle_coord=PRIMARY_ANGLE_COORD.get(canon),
        dynamics_id=dyn,
    )


def all_joint_names() -> tuple[str, ...]:
    return SMPL_X_JOINT_NAMES


def known_tool_joint_names() -> list[str]:
    """Joints that typically have OpenSim angle and/or dynamics coverage."""
    names = set(PRIMARY_ANGLE_COORD) | set(DYNAMICS_JOINT_TO_SMPL_X.values())
    return sorted(names, key=lambda n: _NAME_TO_IDX.get(n, 999))


def is_smplx_joint(name: str) -> bool:
    try:
        resolve_joint_name(name)
        return True
    except KeyError:
        return False


def validate_joint_list(names: Iterable[str]) -> list[str]:
    return [resolve_joint_name(n) for n in names]
