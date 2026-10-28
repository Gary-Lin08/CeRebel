"""Isolated neck-v2 configuration. Production Kinetic Lens files are not imported here."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]
KINETIC_LENS_ROOT = PACKAGE_DIR.parent

RAJAGOPAL_OSIM = KINETIC_LENS_ROOT / "analysis" / "RajagopalLaiUhlrich2023.osim"
GEOMETRY_DIR = KINETIC_LENS_ROOT / "analysis" / "Geometry"
AUGMENTED_OSIM = PACKAGE_DIR / "models" / "RajagopalLaiUhlrich2023_neck.osim"
LOCAL_GEOMETRY_DIR = PACKAGE_DIR / "models" / "geometry"
CALIBRATION_DIR = PACKAGE_DIR / "calibration"
OFFSETS_PATH = CALIBRATION_DIR / "anatomical_offsets.json"
SOMA_TO_OPENSIM_CALIB = CALIBRATION_DIR / "soma_to_opensim_neck.json"
OUTPUTS_DIR = PACKAGE_DIR / "outputs"

# Prefer explicit sessions dir; otherwise the product data dir (CEREBEL_DATA_DIR/sessions).
# Repo-relative data/sessions is only the last resort — playground checkouts often share
# another tree's CEREBEL_DATA_DIR and would otherwise miss gemx.json / hpe_results.pt.
def _sessions_dir() -> Path:
    explicit = os.environ.get("CEREBEL_SESSIONS_DIR", "").strip()
    if explicit:
        return Path(explicit)
    data = os.environ.get("CEREBEL_DATA_DIR", "").strip()
    if data:
        return Path(data) / "sessions"
    return REPO_ROOT / "data" / "sessions"


SESSIONS_DIR = _sessions_dir()
GEMX_ROOT = Path(os.environ.get("CEREBEL_GEMX_ROOT", "/root/GEM-X"))
SOMA_ASSETS = Path(
    os.environ.get("CEREBEL_SOMA_ASSETS", str(GEMX_ROOT / "inputs" / "soma_assets"))
)
GEMX_PYTHON = Path(os.environ.get("CEREBEL_GEMX_PYTHON", str(GEMX_ROOT / ".venv" / "bin" / "python")))
IK_PYTHON = Path(
    os.environ.get("CEREBEL_IK_PYTHON", str(REPO_ROOT / ".venv-ik" / "bin" / "python"))
)

SUBJECT_HEIGHT_CM = float(os.environ.get("CEREBEL_SUBJECT_HEIGHT_CM", "185"))
SUBJECT_MASS_KG = float(os.environ.get("CEREBEL_SUBJECT_MASS_KG", "93"))
GENERIC_VISUAL_HEIGHT_M = 1.75
VISUAL_SCALE = (SUBJECT_HEIGHT_CM / 100.0) / GENERIC_VISUAL_HEIGHT_M

# SOMA77 after dropping the unused Root joint from the 78-name asset list.
SOMA77_NAMES = [
    "Hips",
    "Spine1",
    "Spine2",
    "Chest",
    "Neck1",
    "Neck2",
    "Head",
    "HeadEnd",
    "Jaw",
    "LeftEye",
    "RightEye",
    "LeftShoulder",
    "LeftArm",
    "LeftForeArm",
    "LeftHand",
    "LeftHandThumb1",
    "LeftHandThumb2",
    "LeftHandThumb3",
    "LeftHandThumbEnd",
    "LeftHandIndex1",
    "LeftHandIndex2",
    "LeftHandIndex3",
    "LeftHandIndex4",
    "LeftHandIndexEnd",
    "LeftHandMiddle1",
    "LeftHandMiddle2",
    "LeftHandMiddle3",
    "LeftHandMiddle4",
    "LeftHandMiddleEnd",
    "LeftHandRing1",
    "LeftHandRing2",
    "LeftHandRing3",
    "LeftHandRing4",
    "LeftHandRingEnd",
    "LeftHandPinky1",
    "LeftHandPinky2",
    "LeftHandPinky3",
    "LeftHandPinky4",
    "LeftHandPinkyEnd",
    "RightShoulder",
    "RightArm",
    "RightForeArm",
    "RightHand",
    "RightHandThumb1",
    "RightHandThumb2",
    "RightHandThumb3",
    "RightHandThumbEnd",
    "RightHandIndex1",
    "RightHandIndex2",
    "RightHandIndex3",
    "RightHandIndex4",
    "RightHandIndexEnd",
    "RightHandMiddle1",
    "RightHandMiddle2",
    "RightHandMiddle3",
    "RightHandMiddle4",
    "RightHandMiddleEnd",
    "RightHandRing1",
    "RightHandRing2",
    "RightHandRing3",
    "RightHandRing4",
    "RightHandRingEnd",
    "RightHandPinky1",
    "RightHandPinky2",
    "RightHandPinky3",
    "RightHandPinky4",
    "RightHandPinkyEnd",
    "LeftLeg",
    "LeftShin",
    "LeftFoot",
    "LeftToeBase",
    "LeftToeEnd",
    "RightLeg",
    "RightShin",
    "RightFoot",
    "RightToeBase",
    "RightToeEnd",
]
SOMA77_INDEX = {name: i for i, name in enumerate(SOMA77_NAMES)}

# Drop-Root parents: Hips is root (-1). Chest children include Neck1 and both clavicles.
SOMA77_PARENTS = [
    -1,
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    6,
    6,
    6,
    3,
    11,
    12,
    13,
    14,
    15,
    16,
    17,
    14,
    19,
    20,
    21,
    22,
    14,
    24,
    25,
    26,
    27,
    14,
    29,
    30,
    31,
    32,
    14,
    34,
    35,
    36,
    37,
    3,
    39,
    40,
    41,
    42,
    43,
    44,
    45,
    42,
    47,
    48,
    49,
    50,
    42,
    52,
    53,
    54,
    55,
    42,
    57,
    58,
    59,
    60,
    42,
    62,
    63,
    64,
    65,
    0,
    67,
    68,
    69,
    70,
    0,
    72,
    73,
    74,
    75,
]

# Unscaled Rajagopal torso frame (metres). OpenSim: +X forward, +Y up, +Z right.
NECK_IN_TORSO = (-0.025, 0.418, 0.0)  # C7/T1-region joint
HEAD_IN_NECK = (0.010, 0.090, 0.0)  # C1/skull-region joint
HEAD_IN_TORSO = (
    NECK_IN_TORSO[0] + HEAD_IN_NECK[0],
    NECK_IN_TORSO[1] + HEAD_IN_NECK[1],
    NECK_IN_TORSO[2] + HEAD_IN_NECK[2],
)

# Kinematic-phase inertial split of the original 26.8266 kg torso (includes head).
TORSO_MASS_KG = 21.2266
NECK_MASS_KG = 1.10
HEAD_MASS_KG = 4.50
TORSO_COM = (-0.03, 0.26, 0.0)
NECK_COM = (0.0, 0.04, 0.0)
HEAD_COM = (0.03, 0.07, 0.0)
TORSO_INERTIA = (1.10, 0.55, 1.05, 0.0, 0.0, 0.0)
NECK_INERTIA = (0.008, 0.006, 0.008, 0.0, 0.0, 0.0)
HEAD_INERTIA = (0.022, 0.018, 0.020, 0.0, 0.0, 0.0)

CERVICAL_RANGE_RAD = 1.3962634  # ±80°

# Existing production marker weights (copied, not imported, so production stays frozen).
LEGACY_MARKER_WEIGHTS = {
    "L_HIP": 4.0,
    "R_HIP": 4.0,
    "L_SHOULDER": 3.0,
    "R_SHOULDER": 3.0,
    "L_KNEE": 2.5,
    "R_KNEE": 2.5,
    "L_ANKLE": 2.5,
    "R_ANKLE": 2.5,
    "L_TOE": 1.0,
    "R_TOE": 1.0,
    "L_ELBOW": 2.0,
    "R_ELBOW": 2.0,
    "L_WRIST": 1.5,
    "R_WRIST": 1.5,
}

NECK_MARKER_WEIGHTS = {
    "C7": 4.0,
    "T1": 3.5,
    "JUGULAR": 3.5,
    "L_ACROMION": 2.5,
    "R_ACROMION": 2.5,
    "HEAD_A": 5.0,
    "HEAD_B": 5.0,
    "HEAD_C": 5.0,
}

MARKER_WEIGHTS = {**LEGACY_MARKER_WEIGHTS, **NECK_MARKER_WEIGHTS}

LEGACY_MARKER_SPECS = {
    "L_HIP": ("pelvis", "femur_l"),
    "R_HIP": ("pelvis", "femur_r"),
    "L_SHOULDER": ("torso", "humerus_l"),
    "R_SHOULDER": ("torso", "humerus_r"),
    "L_KNEE": ("tibia_l", "tibia_l"),
    "R_KNEE": ("tibia_r", "tibia_r"),
    "L_ANKLE": ("talus_l", "talus_l"),
    "R_ANKLE": ("talus_r", "talus_r"),
    "L_TOE": ("toes_l", "toes_l"),
    "R_TOE": ("toes_r", "toes_r"),
    "L_ELBOW": ("ulna_l", "ulna_l"),
    "R_ELBOW": ("ulna_r", "ulna_r"),
    "L_WRIST": ("hand_l", "hand_l"),
    "R_WRIST": ("hand_r", "hand_r"),
}

# Local OpenSim marker offsets on the augmented model (unscaled metres, Y-up head frame).
HEAD_MARKER_LOCAL = {
    "HEAD_A": (0.085, 0.110, 0.0),
    "HEAD_B": (0.025, 0.095, 0.075),
    "HEAD_C": (0.025, 0.095, -0.075),
}

NECK_HIERARCHY_CHOICE = "Chest→Neck1 and Neck2→Head"

DEFAULT_SEQUENCE = "1872A5C0-60F0-4C3B-9A1E-000000060001"
