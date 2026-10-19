#!/usr/bin/env python3
"""Enrich Rajagopal motion JSON with MoBL-ARMS upper-limb muscle paths.

MoBL-ARMS is a right-arm model (clavicle/scapula/GH complex, 50 Millard muscles).
It cannot replace Rajagopal full-body IK. Instead we:

1. Build bone-relative marker trajectories from solved Rajagopal bodies
2. Run MoBL IK (right), then mirrored markers (left)
3. Keep only arm-line muscles that stay near the solved bones
4. Merge musculotendon polylines into each motion frame

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

# Marker names must match MoBL_ARMS_41.osim. Clavicle omitted — no Raja body.
MARKER_NAMES = (
    "C7",
    "R.Shoulder",
    "R.Bicep",
    "R.Elbow.Lateral",
    "R.Elbow.Medial",
    "R.Forearm",
    "R.Radius",
    "R.Ulna",
    "Handle",
)

MARKER_WEIGHTS = {
    "C7": 1.5,
    "R.Shoulder": 12.0,
    "R.Bicep": 4.0,
    "R.Elbow.Lateral": 10.0,
    "R.Elbow.Medial": 10.0,
    "R.Forearm": 4.0,
    "R.Radius": 7.0,
    "R.Ulna": 7.0,
    "Handle": 9.0,
}

MOBL_MUSCLE_BASES = {
    "DELT1", "DELT2", "DELT3", "SUPSP", "INFSP", "SUBSC", "TMIN", "TMAJ",
    "PECM1", "PECM2", "PECM3", "LAT1", "LAT2", "LAT3", "CORB", "TRIlong",
    "TRIlat", "TRImed", "ANC", "SUP", "BIClong", "BICshort", "BRA", "BRD",
    "ECRL", "ECRB", "ECU", "FCR", "FCU", "PL", "PT", "PQ", "FDSL", "FDSR",
    "FDSM", "FDSI", "FDPL", "FDPR", "FDPM", "FDPI", "EDCL", "EDCR", "EDCM",
    "EDCI", "EDM", "EIP", "EPL", "EPB", "FPL", "APL",
}

# Scapula/thorax origins + finger/thumb tendons — fly without matching bones.
EXCLUDE_MOBL_MUSCLES = {
    "LAT1", "LAT2", "LAT3",
    "TMAJ", "TMIN",
    "SUBSC", "INFSP", "SUPSP",
    "PECM2", "PECM3",
    "FDSL", "FDSR", "FDSM", "FDSI",
    "FDPL", "FDPR", "FDPM", "FDPI",
    "EDCL", "EDCR", "EDCM", "EDCI",
    "EDM", "EIP", "EPL", "EPB", "FPL", "APL",
}

KEEP_MOBL_MUSCLES = MOBL_MUSCLE_BASES - EXCLUDE_MOBL_MUSCLES

MAX_MEDIAN_BONE_DIST_M = 0.075
MAX_POINT_BONE_DIST_M = 0.14

ARM_BODIES_R = ("torso", "humerus_r", "ulna_r", "radius_r", "hand_r")
ARM_BODIES_L = ("torso", "humerus_l", "ulna_l", "radius_l", "hand_l")


def available() -> bool:
    return MOBL_OSIM.is_file() and MOBL_GEOMETRY.is_dir()


def _body_pq(frame_bodies: dict, name: str) -> tuple[np.ndarray, np.ndarray] | None:
    b = frame_bodies.get(name)
    if not b:
        return None
    p = np.asarray(b.get("p") or [0, 0, 0], dtype=np.float64)
    q = np.asarray(b.get("q") or [0, 0, 0, 1], dtype=np.float64)
    return p, q


def _quat_rotate(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    x, y, z, w = [float(c) for c in q]
    qv = np.array([x, y, z], dtype=np.float64)
    t = 2.0 * np.cross(qv, v)
    return v + w * t + np.cross(qv, t)


def _bone_axis(prox: np.ndarray, dist: np.ndarray) -> tuple[np.ndarray, float]:
    d = dist - prox
    length = float(np.linalg.norm(d))
    if length < 1e-6:
        return np.array([0.0, -1.0, 0.0]), 0.0
    return d / length, length


def _ortho_frame(axis: np.ndarray, hint: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (lateral, anterior) unit vectors given a distal bone axis."""
    a = axis / max(float(np.linalg.norm(axis)), 1e-12)
    h = hint - a * float(np.dot(hint, a))
    if float(np.linalg.norm(h)) < 1e-6:
        h = np.array([1.0, 0.0, 0.0]) - a * a[0]
    if float(np.linalg.norm(h)) < 1e-6:
        h = np.array([0.0, 0.0, 1.0]) - a * a[2]
    lateral = h / max(float(np.linalg.norm(h)), 1e-12)
    anterior = np.cross(lateral, a)
    n = float(np.linalg.norm(anterior))
    if n < 1e-6:
        anterior = np.array([0.0, 0.0, 0.0])
    else:
        anterior = anterior / n
    return lateral, anterior


def markers_from_raja_frame(frame_bodies: dict, *, side: str) -> dict[str, np.ndarray]:
    """Bone-relative MoBL markers from Rajagopal body origins (scale-invariant)."""
    s = "r" if side == "r" else "l"
    torso = _body_pq(frame_bodies, "torso")
    hum = _body_pq(frame_bodies, f"humerus_{s}")
    ulna = _body_pq(frame_bodies, f"ulna_{s}")
    radius = _body_pq(frame_bodies, f"radius_{s}")
    hand = _body_pq(frame_bodies, f"hand_{s}")
    if not all((torso, hum, ulna, radius, hand)):
        return {}

    torso_p, torso_q = torso
    hum_p, _ = hum
    ulna_p, _ = ulna
    radius_p, _ = radius
    hand_p, _ = hand

    hum_axis, hum_len = _bone_axis(hum_p, ulna_p)
    fore_axis, fore_len = _bone_axis(ulna_p, hand_p)
    # Prefer world +Z as lateral hint on the right, -Z on the left.
    lat_hint = np.array([0.0, 0.0, 1.0 if s == "r" else -1.0])
    hum_lat, hum_ant = _ortho_frame(hum_axis, lat_hint)
    fore_lat, fore_ant = _ortho_frame(fore_axis, lat_hint)

    # Elbow width ~ scaled by humeral length; forearm offsets similarly.
    elbow_half = max(0.02, 0.12 * hum_len)
    out = {
        "C7": torso_p + _quat_rotate(torso_q, np.array([-0.04, 0.18, 0.0])),
        "R.Shoulder": hum_p - hum_axis * (0.02 * max(hum_len, 0.2)),
        "R.Bicep": hum_p + hum_axis * (0.45 * hum_len) + hum_ant * 0.02,
        "R.Elbow.Lateral": ulna_p + hum_lat * elbow_half,
        "R.Elbow.Medial": ulna_p - hum_lat * elbow_half,
        "R.Forearm": ulna_p + fore_axis * (0.40 * fore_len) + fore_ant * 0.015,
        "R.Radius": radius_p + fore_axis * (0.55 * fore_len) + fore_lat * 0.015,
        "R.Ulna": ulna_p + fore_axis * (0.70 * fore_len) - fore_lat * 0.015,
        "Handle": hand_p + fore_axis * 0.03,
    }
    if side == "l":
        # Drive the right-handed MoBL model with a sagittal mirror.
        out = {k: np.array([v[0], v[1], -v[2]], dtype=np.float64) for k, v in out.items()}
    return out


def _write_trc(path: Path, times: list[float], targets: list[dict[str, np.ndarray]], fps: float) -> None:
    names = list(MARKER_NAMES)
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


def _segment_distances(points: np.ndarray, origins: list[np.ndarray]) -> np.ndarray:
    if not origins:
        return np.full(len(points), 1e3)
    O = np.stack(origins)
    d = np.linalg.norm(points[:, None, :] - O[None, :, :], axis=-1).min(axis=1)
    if len(origins) < 2:
        return d
    seg_d = np.empty(len(points))
    for i, p in enumerate(points):
        best = 1e9
        for a, b in zip(origins, origins[1:]):
            ab = b - a
            denom = float(np.dot(ab, ab))
            t = 0.0 if denom < 1e-12 else float(np.clip(np.dot(p - a, ab) / denom, 0.0, 1.0))
            best = min(best, float(np.linalg.norm(p - (a + t * ab))))
        seg_d[i] = best
    return np.minimum(d, seg_d)


def _body_origins(frame_bodies: dict, names: tuple[str, ...]) -> list[np.ndarray]:
    out: list[np.ndarray] = []
    for name in names:
        b = frame_bodies.get(name)
        if not b:
            continue
        out.append(np.asarray(b.get("p") or [0, 0, 0], dtype=np.float64))
    return out


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
            base = muscle.getName()
            if base not in KEEP_MOBL_MUSCLES:
                continue
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
                paths.append({"name": f"{base}{name_suffix}", "points": points})
        per_frame.append(paths)
    return per_frame


def _filter_paths_near_bones(
    paths_per_frame: list[list[dict[str, Any]]],
    frames: list[dict],
    *,
    side: str,
) -> tuple[list[list[dict[str, Any]]], list[str]]:
    body_names = ARM_BODIES_R if side == "r" else ARM_BODIES_L
    sample_idx = list(range(0, len(frames), max(1, len(frames) // 20)))
    scores: dict[str, list[tuple[float, float]]] = {}
    for i in sample_idx:
        origins = _body_origins(frames[i].get("bodies") or {}, body_names)
        idx = min(i, len(paths_per_frame) - 1)
        for muscle in paths_per_frame[idx]:
            pts = np.asarray(muscle.get("points") or [], dtype=np.float64)
            if len(pts) < 2:
                continue
            dist = _segment_distances(pts, origins)
            scores.setdefault(muscle["name"], []).append((float(np.median(dist)), float(np.max(dist))))

    keep: set[str] = set()
    dropped: list[str] = []
    for name, vals in scores.items():
        med = float(np.mean([v[0] for v in vals]))
        mx = float(np.mean([v[1] for v in vals]))
        if med <= MAX_MEDIAN_BONE_DIST_M and mx <= MAX_POINT_BONE_DIST_M:
            keep.add(name)
        else:
            dropped.append(f"{name}(med={med:.3f},max={mx:.3f})")

    filtered = [[m for m in frame_paths if m["name"] in keep] for frame_paths in paths_per_frame]
    return filtered, sorted(dropped)


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

    for frame in frames:
        muscles = frame.get("muscles") or []
        frame["muscles"] = [
            m
            for m in muscles
            if not (
                (str(m.get("name", "")).endswith("_r") or str(m.get("name", "")).endswith("_l"))
                and str(m.get("name", ""))[:-2] in MOBL_MUSCLE_BASES
            )
        ]

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
        _ = osim.Model(str(MOBL_OSIM))
    except Exception:
        LOG.exception("OpenSim failed to load MoBL-ARMS")
        return motion

    merged_counts = {"r": 0, "l": 0}
    dropped_all: list[str] = []
    try:
        for side, suffix, mirror in (("r", "_r", False), ("l", "_l", True)):
            targets = [markers_from_raja_frame(f.get("bodies") or {}, side=side) for f in frames]
            if not targets or len(targets[0]) < 4:
                LOG.warning("insufficient Rajagopal bodies for MoBL side=%s", side)
                continue
            trc = work_dir / f"mobl-markers-{side}.trc"
            mot = work_dir / f"mobl-ik-{side}.mot"
            _write_trc(trc, times, targets, fps)
            model_copy = work_dir / f"MOBL_ARMS_41_{side}.osim"
            shutil.copy2(MOBL_OSIM, model_copy)
            _run_mobl_ik(model_copy, trc, mot, times)
            paths = _muscle_paths_from_mot(model_copy, mot, name_suffix=suffix, mirror_z=mirror)
            paths, dropped = _filter_paths_near_bones(paths, frames, side=side)
            dropped_all.extend(dropped)
            for i, frame in enumerate(frames):
                idx = min(i, len(paths) - 1)
                frame["muscles"] = list(frame.get("muscles") or []) + paths[idx]
            merged_counts[side] = len(paths[0]) if paths else 0

        meta = motion.setdefault("metadata", {})
        if frames and frames[0].get("muscles"):
            meta["muscleCount"] = len(frames[0]["muscles"])
        meta["moblArms"] = {
            "enabled": True,
            "model": "MOBL_ARMS_41",
            "source": "https://github.com/CEINMS-RT/UpperLimbModel",
            "license": "non-commercial BSD 3-Clause (see kinetic-lens/third_party/MoBL-ARMS/NOTICE)",
            "rightMuscles": merged_counts["r"],
            "leftMuscles": merged_counts["l"],
            "kept": sorted(KEEP_MOBL_MUSCLES),
            "excluded": sorted(EXCLUDE_MOBL_MUSCLES),
            "droppedProximity": dropped_all,
            "note": (
                "Arm-line MoBL-ARMS paths only (deltoid/bi/tri/forearm). "
                "Scapular/thoracic and finger muscles excluded; proximity-filtered."
            ),
        }
        warn = meta.get("warning") or ""
        extra = " Includes MoBL-ARMS upper-limb muscle paths (visualization / non-commercial)."
        if extra.strip() not in warn:
            meta["warning"] = (warn + extra).strip()
        LOG.info(
            "MoBL-ARMS enrich ok right=%d left=%d dropped=%d totalMuscles=%s",
            merged_counts["r"],
            merged_counts["l"],
            len(dropped_all),
            meta.get("muscleCount"),
        )
        if dropped_all:
            LOG.info("MoBL proximity drops: %s", dropped_all)
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
