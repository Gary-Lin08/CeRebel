"""Dynamic anatomical markers = legacy body markers + neck/head landmarks."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from .anatomical_landmarks import LANDMARK_NAMES, landmark_trajectories, load_or_init_offsets
from .config import (
    HEAD_MARKER_LOCAL,
    LEGACY_MARKER_SPECS,
    MARKER_WEIGHTS,
    PACKAGE_DIR,
    VISUAL_SCALE,
)
from .soma_frames import SomaSequence


def _repo_on_path() -> None:
    root = str(PACKAGE_DIR.parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)


def legacy_targets_from_pose3d(pose3d: dict) -> tuple[list[float], list[dict[str, np.ndarray]]]:
    _repo_on_path()
    from cerebel_ik_retarget import direct_segment_targets

    return direct_segment_targets(pose3d)


def combined_targets(
    pose3d: dict,
    seq: SomaSequence,
) -> tuple[list[float], list[dict[str, np.ndarray]]]:
    times, legacy = legacy_targets_from_pose3d(pose3d)
    n = min(len(legacy), seq.joints_opensim.shape[0])
    landmarks = landmark_trajectories(seq)
    merged: list[dict[str, np.ndarray]] = []
    for t in range(n):
        frame = {k: v.copy() for k, v in legacy[t].items()}
        for name in LANDMARK_NAMES:
            frame[name] = landmarks[name][t].copy()
        merged.append(frame)
    return times[:n], merged


def write_trc(path: Path, times: list[float], targets: list[dict[str, np.ndarray]], fps: float) -> None:
    names = list(LEGACY_MARKER_SPECS) + list(LANDMARK_NAMES)
    rows = [
        f"PathFileType\t4\t(X/Y/Z)\t{path.name}",
        "DataRate\tCameraRate\tNumFrames\tNumMarkers\tUnits\tOrigDataRate\tOrigDataStartFrame\tOrigNumFrames",
        f"{fps:.8f}\t{fps:.8f}\t{len(times)}\t{len(names)}\tm\t{fps:.8f}\t1\t{len(times)}",
        "Frame#\tTime\t" + "\t\t\t".join(names),
        "\t\t" + "\t".join(f"X{index}\tY{index}\tZ{index}" for index in range(1, len(names) + 1)),
        "",
    ]
    for index, (time, frame) in enumerate(zip(times, targets), start=1):
        values = [f"{index}", f"{time:.8f}"]
        for name in names:
            values.extend(f"{float(v):.8f}" for v in frame[name])
        rows.append("\t".join(values))
    path.write_text("\n".join(rows) + "\n")


def head_markers_collinear(targets: list[dict[str, np.ndarray]], *, eps: float = 1e-4) -> bool:
    pts = np.stack(
        [np.array([t["HEAD_A"], t["HEAD_B"], t["HEAD_C"]], dtype=np.float64) for t in targets[:1]]
    )[0]
    area = np.linalg.norm(np.cross(pts[1] - pts[0], pts[2] - pts[0]))
    return float(area) < eps


def attach_markers(model, output_path: Path) -> None:
    import opensim as osim

    model.finalizeConnections()
    state = model.initSystem()
    markers = model.getMarkerSet()
    while markers.getSize() > 0:
        markers.remove(0)
    state = model.initSystem()
    bodies = model.getBodySet()
    origins: dict[str, np.ndarray] = {}
    for index in range(bodies.getSize()):
        body = bodies.get(index)
        p = body.getPositionInGround(state)
        origins[body.getName()] = np.array([float(p.get(i)) for i in range(3)])

    for marker_name, (parent_name, reference_name) in LEGACY_MARKER_SPECS.items():
        parent = bodies.get(parent_name)
        if parent_name == reference_name:
            local = osim.Vec3(0)
        else:
            delta = origins[reference_name] - origins[parent_name]
            local = osim.Vec3(*(float(v) for v in delta))
        model.addMarker(osim.Marker(marker_name, parent, local))

    neck = bodies.get("neck")
    torso = bodies.get("torso")
    head = bodies.get("head")
    model.addMarker(osim.Marker("C7", neck, osim.Vec3(0)))
    t1_local = origins["neck"] - origins["torso"]
    t1_local[0] -= 0.02 * VISUAL_SCALE
    model.addMarker(osim.Marker("T1", torso, osim.Vec3(*(float(v) for v in t1_local))))
    jug = osim.Vec3(0.06 * VISUAL_SCALE, 0.32 * VISUAL_SCALE, 0.0)
    model.addMarker(osim.Marker("JUGULAR", torso, jug))
    l_acr = origins["humerus_l"] - origins["torso"]
    r_acr = origins["humerus_r"] - origins["torso"]
    model.addMarker(osim.Marker("L_ACROMION", torso, osim.Vec3(*(float(v) for v in l_acr))))
    model.addMarker(osim.Marker("R_ACROMION", torso, osim.Vec3(*(float(v) for v in r_acr))))
    for name, offset in HEAD_MARKER_LOCAL.items():
        scaled = osim.Vec3(*(float(v) * VISUAL_SCALE for v in offset))
        model.addMarker(osim.Marker(name, head, scaled))

    model.finalizeConnections()
    model.initSystem()
    model.printToXML(str(output_path))


def dump_marker_config(path: Path) -> None:
    offsets = load_or_init_offsets()
    path.write_text(
        json.dumps(
            {
                "legacy_markers": list(LEGACY_MARKER_SPECS),
                "neck_markers": list(LANDMARK_NAMES),
                "weights": MARKER_WEIGHTS,
                "anatomical_offsets": offsets,
            },
            indent=2,
        )
        + "\n"
    )
