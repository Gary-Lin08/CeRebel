#!/usr/bin/env python3
"""Enrich Rajagopal motion JSON with MoBL-ARMS upper-limb muscle paths.

MoBL-ARMS is a right-arm model (clavicle/scapula/GH complex, 50 Millard muscles).
It cannot replace Rajagopal full-body IK. Instead we:

1. Build virtual marker trajectories from solved Rajagopal body transforms
2. Run OpenSim IK on MoBL-ARMS (right), then again on mirrored markers (left)
3. Append musculotendon path polylines into each motion frame

Soft-fails: on any error the original motion is returned unchanged.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np

LOG = logging.getLogger("cerebel.mobl_arms")

MOBL_ROOT = Path(__file__).resolve().parents[1] / "third_party" / "MoBL-ARMS"
MOBL_OSIM = MOBL_ROOT / "MOBL_ARMS_41.osim"
MOBL_GEOMETRY = MOBL_ROOT / "Geometry"

# Markers present on MoBL_ARMS_41.osim that we can drive from Rajagopal bodies.
# Values are (raja_body, local_offset_in_body_frame_m).
RIGHT_MARKER_SOURCES: dict[str, tuple[str, tuple[float, float, float]]] = {
    "C7": ("torso", (-0.04, 0.18, 0.0)),
    "R.Clavicle": ("torso", (0.02, 0.14, 0.06)),
    "R.Shoulder": ("humerus_r", (0.0, 0.0, 0.0)),
    "R.Bicep": ("humerus_r", (0.02, -0.14, 0.0)),
    "R.Elbow.Lateral": ("ulna_r", (0.0, 0.0, 0.03)),
    "R.Elbow.Medial": ("ulna_r", (0.0, 0.0, -0.03)),
    "R.Forearm": ("radius_r", (0.02, -0.08, 0.0)),
    "R.Radius": ("radius_r", (0.0, -0.12, 0.02)),
    "R.Ulna": ("ulna_r", (0.0, -0.14, -0.02)),
    "Handle": ("hand_r", (0.02, -0.04, 0.0)),
}

LEFT_BODY_MAP = {
    "torso": "torso",
    "humerus_r": "humerus_l",
    "ulna_r": "ulna_l",
    "radius_r": "radius_l",
    "hand_r": "hand_l",
}

MARKER_WEIGHTS = {
    "C7": 2.0,
    "R.Clavicle": 1.0,
    "R.Shoulder": 8.0,
    "R.Bicep": 2.0,
    "R.Elbow.Lateral": 6.0,
    "R.Elbow.Medial": 6.0,
    "R.Forearm": 2.0,
    "R.Radius": 4.0,
    "R.Ulna": 4.0,
    "Handle": 5.0,
}

MOBL_MUSCLE_BASES = {
    "DELT1", "DELT2", "DELT3", "SUPSP", "INFSP", "SUBSC", "TMIN", "TMAJ",
    "PECM1", "PECM2", "PECM3", "LAT1", "LAT2", "LAT3", "CORB", "TRIlong",
    "TRIlat", "TRImed", "ANC", "SUP", "BIClong", "BICshort", "BRA", "BRD",
    "ECRL", "ECRB", "ECU", "FCR", "FCU", "PL", "PT", "PQ", "FDSL", "FDSR",
    "FDSM", "FDSI", "FDPL", "FDPR", "FDPM", "FDPI", "EDCL", "EDCR", "EDCM",
    "EDCI", "EDM", "EIP", "EPL", "EPB", "FPL", "APL",
}


def available() -> bool:
    return MOBL_OSIM.is_file() and MOBL_GEOMETRY.is_dir()


def _quat_rotate(q: list[float] | np.ndarray, v: np.ndarray) -> np.ndarray:
    """Rotate vector v by quaternion q = [x, y, z, w]."""
    x, y, z, w = [float(c) for c in q]
    qv = np.array([x, y, z], dtype=np.float64)
    t = 2.0 * np.cross(qv, v)
    return v + w * t + np.cross(qv, t)


def _body_point(frame_bodies: dict, body: str, local: tuple[float, float, float]) -> np.ndarray | None:
    b = frame_bodies.get(body)
    if not b:
        return None
    p = np.asarray(b.get("p") or [0, 0, 0], dtype=np.float64)
    q = b.get("q") or [0, 0, 0, 1]
    return p + _quat_rotate(q, np.asarray(local, dtype=np.float64))


def markers_from_raja_frame(frame_bodies: dict, *, side: str) -> dict[str, np.ndarray]:
    """Build MoBL marker world positions from a Rajagopal body frame."""
    out: dict[str, np.ndarray] = {}
    for marker, (body_r, local) in RIGHT_MARKER_SOURCES.items():
        body = body_r if side == "r" else LEFT_BODY_MAP[body_r]
        # Mirror local Z for the left arm so lateral/medial stay anatomically sided.
        loc = local if side == "r" else (local[0], local[1], -local[2])
        pt = _body_point(frame_bodies, body, loc)
        if pt is None:
            continue
        if side == "l":
            pt = pt.copy()
            pt[2] = -pt[2]
        out[marker] = pt
    return out


def _write_trc(path: Path, times: list[float], targets: list[dict[str, np.ndarray]], fps: float) -> None:
    names = list(RIGHT_MARKER_SOURCES)
    rows = [
        f"PathFileType\t4\t(X/Y/Z)\t{path.name}",
        "DataRate\tCameraRate\tNumFrames\tNumMarkers\tUnits\tOrigDataRate\tOrigDataStartFrame\tOrigNumFrames",
        f"{fps:.8f}\t{fps:.8f}\t{len(times)}\t{len(names)}\tm\t{fps:.8f}\t1\t{len(times)}",
        "Frame#\tTime\t" + "\t\t\t".join(names),
        "\t\t" + "\t".join(f"X{i}\tY{i}\tZ{i}" for i in range(1, len(names) + 1)),
        "",
    ]
    for index, (time, frame) in enumerate(zip(times, targets), start=1):
        values = [f"{index}", f"{time:.8f}"]
        for name in names:
            pt = frame.get(name, np.zeros(3))
            values.extend(f"{float(v):.8f}" for v in pt)
        rows.append("\t".join(values))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _run_mobl_ik(model_path: Path, trc_path: Path, mot_path: Path, times: list[float]) -> None:
    import opensim as osim

    osim.ModelVisualizer.addDirToGeometrySearchPaths(str(MOBL_GEOMETRY))
    tool = osim.InverseKinematicsTool()
    tool.setName("MoBL_ARMS_upper_enrich")
    model = osim.Model(str(model_path))
    tool.setModel(model)
    tool.setMarkerDataFileName(str(trc_path))
    tool.setStartTime(float(times[0]))
    tool.setEndTime(float(times[-1]))
    tool.setOutputMotionFileName(str(mot_path))
    tool.set_accuracy(1e-5)
    tool.set_report_errors(True)
    task_set = tool.upd_IKTaskSet()
    for name, weight in MARKER_WEIGHTS.items():
        task = osim.IKMarkerTask()
        task.setName(name)
        task.setApply(True)
        task.setWeight(float(weight))
        task_set.cloneAndAppend(task)
    if not tool.run():
        raise RuntimeError("MoBL-ARMS InverseKinematicsTool failed")


def _read_mot(path: Path) -> tuple[list[str], np.ndarray, np.ndarray]:
    import opensim as osim

    table = osim.TimeSeriesTable(str(path))
    labels = list(table.getColumnLabels())
    times = np.array([table.getIndependentColumn()[i] for i in range(table.getNumRows())], dtype=np.float64)
    values = np.array(
        [[table.getDependentColumnAtIndex(c)[r] for c in range(table.getNumColumns())] for r in range(table.getNumRows())],
        dtype=np.float64,
    )
    return labels, times, values


def _muscle_paths_from_mot(
    model_path: Path,
    mot_path: Path,
    *,
    name_suffix: str,
    mirror_z: bool,
) -> list[list[dict[str, Any]]]:
    import opensim as osim

    osim.ModelVisualizer.addDirToGeometrySearchPaths(str(MOBL_GEOMETRY))
    model = osim.Model(str(model_path))
    state = model.initSystem()
    labels, times, values = _read_mot(mot_path)
    coordinates = model.getCoordinateSet()
    lookup = {coordinates.get(i).getName(): coordinates.get(i) for i in range(coordinates.getSize())}
    muscles = model.getMuscles()
    per_frame: list[list[dict[str, Any]]] = []

    for row in range(len(times)):
        for column, label in enumerate(labels):
            coordinate_name = label.rsplit("/", 2)[-2] if label.endswith("/value") else label
            coordinate = lookup.get(coordinate_name)
            if coordinate is None:
                continue
            value = float(values[row, column])
            if coordinate.getMotionType() == osim.Coordinate.Rotational:
                value = math.radians(value)
            coordinate.setValue(state, value, False)
        model.realizePosition(state)
        paths: list[dict[str, Any]] = []
        for mi in range(muscles.getSize()):
            muscle = muscles.get(mi)
            pts_set = muscle.getGeometryPath().getPathPointSet()
            points = []
            for pi in range(pts_set.getSize()):
                try:
                    loc = pts_set.get(pi).getLocationInGround(state)
                    pt = [float(loc.get(0)), float(loc.get(1)), float(loc.get(2))]
                except Exception:
                    continue
                if mirror_z:
                    pt[2] = -pt[2]
                points.append([round(c, 4) for c in pt])
            if len(points) >= 2:
                paths.append({"name": f"{muscle.getName()}{name_suffix}", "points": points})
        per_frame.append(paths)
    return per_frame


def enrich_opensim_motion(
    motion: dict[str, Any],
    work_dir: Path,
    *,
    enabled: bool = True,
) -> dict[str, Any]:
    """Mutate/return motion with MoBL upper-limb muscle paths merged in."""
    if not enabled:
        return motion
    if not available():
        LOG.warning("MoBL-ARMS assets missing under %s — skipping upper enrich", MOBL_ROOT)
        return motion

    frames = motion.get("frames") or []
    if not frames:
        return motion

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    times = [float(f.get("time", 0.0)) for f in frames]
    if len(times) >= 2:
        fps = 1.0 / max(1e-6, float(np.median(np.diff(times))))
    else:
        fps = float((motion.get("metadata") or {}).get("fps") or 30.0)

    try:
        import opensim as osim  # noqa: F401

        osim.ModelVisualizer.addDirToGeometrySearchPaths(str(MOBL_GEOMETRY))
        # Ensure model loads once (also upgrades XML in-memory).
        _ = osim.Model(str(MOBL_OSIM))
    except Exception:
        LOG.exception("OpenSim failed to load MoBL-ARMS")
        return motion

    merged_counts = {"r": 0, "l": 0}
    try:
        for side, suffix, mirror in (("r", "_r", False), ("l", "_l", True)):
            targets = [markers_from_raja_frame(f.get("bodies") or {}, side=side) for f in frames]
            if not targets or len(targets[0]) < 4:
                LOG.warning("insufficient Rajagopal bodies for MoBL side=%s", side)
                continue
            trc = work_dir / f"mobl-markers-{side}.trc"
            mot = work_dir / f"mobl-ik-{side}.mot"
            _write_trc(trc, times, targets, fps)
            # Fresh copy so OpenSim does not mutate the vendored file.
            model_copy = work_dir / f"MOBL_ARMS_41_{side}.osim"
            shutil.copy2(MOBL_OSIM, model_copy)
            _run_mobl_ik(model_copy, trc, mot, times)
            paths = _muscle_paths_from_mot(model_copy, mot, name_suffix=suffix, mirror_z=mirror)
            if len(paths) != len(frames):
                # Resample by nearest time if OpenSim dropped frames.
                LOG.warning(
                    "MoBL side=%s frame mismatch mot=%d motion=%d — nearest-neighbour stitch",
                    side,
                    len(paths),
                    len(frames),
                )
            for i, frame in enumerate(frames):
                idx = min(i, len(paths) - 1)
                existing = list(frame.get("muscles") or [])
                existing = [
                    m
                    for m in existing
                    if not (
                        str(m.get("name", "")).endswith(suffix)
                        and str(m.get("name", ""))[: -len(suffix)] in MOBL_MUSCLE_BASES
                    )
                ]
                existing.extend(paths[idx])
                frame["muscles"] = existing
            merged_counts[side] = len(paths[0]) if paths else 0

        meta = motion.setdefault("metadata", {})
        raja_count = int(meta.get("muscleCount") or 0)
        mobl_total = merged_counts["r"] + merged_counts["l"]
        # Recount from frame 0 for accuracy.
        if frames and frames[0].get("muscles"):
            meta["muscleCount"] = len(frames[0]["muscles"])
        else:
            meta["muscleCount"] = raja_count + mobl_total
        meta["moblArms"] = {
            "enabled": True,
            "model": "MOBL_ARMS_41",
            "source": "https://github.com/CEINMS-RT/UpperLimbModel",
            "license": "non-commercial BSD 3-Clause (see kinetic-lens/third_party/MoBL-ARMS/NOTICE)",
            "rightMuscles": merged_counts["r"],
            "leftMuscles": merged_counts["l"],
            "note": (
                "Upper-limb musculotendon paths from MoBL-ARMS driven by Rajagopal "
                "arm body poses. Shoulder complex (clavicle/scapula) is MoBL-native; "
                "not a full skeletal graft into Rajagopal."
            ),
        }
        warn = meta.get("warning") or ""
        extra = " Includes MoBL-ARMS upper-limb muscle paths (visualization / non-commercial)."
        if extra.strip() not in warn:
            meta["warning"] = (warn + extra).strip()
        LOG.info(
            "MoBL-ARMS enrich ok right=%d left=%d totalMuscles=%s",
            merged_counts["r"],
            merged_counts["l"],
            meta.get("muscleCount"),
        )
    except Exception:
        LOG.exception("MoBL-ARMS enrich failed — keeping Rajagopal muscles only")
    return motion


def enrich_motion_file(motion_path: Path, work_dir: Path | None = None) -> bool:
    motion_path = Path(motion_path)
    if not motion_path.is_file():
        return False
    motion = json.loads(motion_path.read_text(encoding="utf-8"))
    work = work_dir or (motion_path.parent / "mobl-work")
    enrich_opensim_motion(motion, work)
    motion_path.write_text(json.dumps(motion, separators=(",", ":")), encoding="utf-8")
    return bool((motion.get("metadata") or {}).get("moblArms", {}).get("enabled"))


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--motion", type=Path, required=True)
    p.add_argument("--work-dir", type=Path, default=None)
    args = p.parse_args()
    ok = enrich_motion_file(args.motion, args.work_dir)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
