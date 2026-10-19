"""Sanity checks for the isolated neck-v2 pipeline."""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from .anatomical_landmarks import LANDMARK_NAMES
from .config import AUGMENTED_OSIM, LEGACY_MARKER_SPECS
from .generate_markers import head_markers_collinear
from .rotations import geodesic_deg, matrix_from_quat_xyzw


class CheckError(RuntimeError):
    pass


def check_model(model_path: Path | None = None) -> list[str]:
    path = model_path or AUGMENTED_OSIM
    root = ET.parse(path).getroot()
    bodies = {b.attrib["name"] for b in root.findall(".//BodySet/objects/Body")}
    joints = {j.attrib.get("name") for j in root.findall(".//JointSet/objects/*")}
    if "neck" not in bodies or "head" not in bodies:
        raise CheckError(f"missing neck/head bodies: {bodies}")
    if "cervical_lower" not in joints or "cervical_upper" not in joints:
        raise CheckError(f"missing cervical joints: {joints}")
    coords = [c.attrib["name"] for c in root.findall(".//Coordinate")]
    for name in (
        "neck_extension",
        "neck_bending",
        "neck_rotation",
        "head_extension",
        "head_bending",
        "head_rotation",
    ):
        if name not in coords:
            raise CheckError(f"missing coordinate {name}")
    # No translational cervical coordinates.
    for joint in root.findall(".//JointSet/objects/CustomJoint"):
        if joint.attrib.get("name") not in ("cervical_lower", "cervical_upper"):
            continue
        for axis in joint.findall("./SpatialTransform/TransformAxis"):
            if not str(axis.attrib.get("name", "")).startswith("translation"):
                continue
            coord = (axis.find("coordinates") or axis.find("coordinates")).text
            if coord and coord.strip():
                raise CheckError(f"cervical translational DOF on {joint.attrib['name']}")
    torso = next(b for b in root.findall(".//BodySet/objects/Body") if b.attrib["name"] == "torso")
    torso_meshes = [m.find("mesh_file").text for m in torso.findall("./attached_geometry/Mesh")]
    if "hat_skull.vtp" in torso_meshes or "hat_jaw.vtp" in torso_meshes:
        raise CheckError("head geometry still attached to torso")
    head = next(b for b in root.findall(".//BodySet/objects/Body") if b.attrib["name"] == "head")
    head_meshes = [m.find("mesh_file").text for m in head.findall("./attached_geometry/Mesh")]
    if not any("skull" in (m or "") for m in head_meshes):
        raise CheckError("head body missing skull geometry")
    neck = next(b for b in root.findall(".//BodySet/objects/Body") if b.attrib["name"] == "neck")
    if not neck.findall("./attached_geometry/Mesh"):
        raise CheckError("neck geometry missing")
    return ["model_ok"]


def check_markers(targets: list[dict]) -> list[str]:
    names = set(targets[0])
    missing = [n for n in list(LEGACY_MARKER_SPECS) + list(LANDMARK_NAMES) if n not in names]
    if missing:
        raise CheckError(f"missing markers {missing}")
    if head_markers_collinear(targets):
        raise CheckError("HEAD_A/B/C are collinear")
    for frame in targets:
        for name, xyz in frame.items():
            if not np.all(np.isfinite(xyz)):
                raise CheckError(f"NaN in marker {name}")
    return ["markers_ok"]


def check_motion(motion: dict, times: list[float]) -> list[str]:
    frames = motion["frames"]
    if abs(float(frames[0]["time"]) - float(times[0])) > 1e-3:
        raise CheckError("IK start time mismatch")
    if abs(float(frames[-1]["time"]) - float(times[-1])) > 2e-2:
        raise CheckError("IK end time mismatch")
    if "neck" not in motion["bodyNames"] or "head" not in motion["bodyNames"]:
        raise CheckError("exported motion missing neck/head")
    for frame in frames:
        for v in frame["coordinates"].values():
            if isinstance(v, float) and math.isnan(v):
                raise CheckError("NaN coordinate")
    # Continuity: successive orientation jumps should stay well below 360°.
    for name in ("pelvis", "torso", "neck", "head"):
        prev = None
        for frame in frames:
            r = matrix_from_quat_xyzw(frame["bodies"][name]["q"])
            if prev is not None:
                jump = geodesic_deg(prev, r)
                if jump > 90.0:
                    raise CheckError(f"{name} orientation jump {jump:.1f} deg")
            prev = r
    return ["ik_ok", "continuity_ok"]


def write_check_report(path: Path, items: list[str]) -> None:
    path.write_text(json.dumps({"passed": items}, indent=2) + "\n")
