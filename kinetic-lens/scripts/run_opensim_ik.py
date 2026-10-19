#!/usr/bin/env python3
"""Run height-scaled OpenSim inverse kinematics from monocular 3D landmarks.

The 3D landmarks are first converted into a fixed-segment virtual marker
skeleton. OpenSim then solves a weighted least-squares IK problem at every
video frame. This is a real OpenSim IK pass, with monocular marker estimates
instead of optical motion-capture markers.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import opensim as osim

from export_opensim_assets import (
    VISUAL_SCALE,
    export_glb,
    rotation_to_quaternion,
)


SUBJECT_HEIGHT_M = 1.85
SUBJECT_MASS_KG = 93.0

MARKER_SPECS = {
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

LANDMARK_INDEX = {
    "L_SHOULDER": 11,
    "R_SHOULDER": 12,
    "L_ELBOW": 13,
    "R_ELBOW": 14,
    "L_WRIST": 15,
    "R_WRIST": 16,
    "L_HIP": 23,
    "R_HIP": 24,
    "L_KNEE": 25,
    "R_KNEE": 26,
    "L_ANKLE": 27,
    "R_ANKLE": 28,
    "L_TOE": 31,
    "R_TOE": 32,
}

MARKER_WEIGHTS = {
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


def as_array(value) -> np.ndarray:
    return np.array([float(value.get(i)) for i in range(3)], dtype=np.float64)


def model_vec3(value) -> list[float]:
    """Export positions from the already-scaled OpenSim model without re-scaling."""

    return [round(float(value.get(i)), 6) for i in range(3)]


def unit(vector: np.ndarray, fallback: np.ndarray) -> np.ndarray:
    length = float(np.linalg.norm(vector))
    if length < 1e-8:
        return fallback.copy()
    return vector / length


def distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.linalg.norm(a - b))


def mp_to_opensim(points: np.ndarray) -> np.ndarray:
    """Map MediaPipe camera axes to OpenSim ground axes.

    Camera-right becomes OpenSim +X, camera-up becomes +Y, and camera-forward
    becomes +Z. The IK solver recovers anatomical yaw from named left/right
    marker relationships.
    """

    output = np.empty_like(points)
    output[:, 0] = points[:, 0]
    output[:, 1] = -points[:, 1]
    output[:, 2] = -points[:, 2]
    return output


def body_origins(model, state) -> dict[str, np.ndarray]:
    bodies = model.getBodySet()
    return {
        bodies.get(index).getName(): as_array(bodies.get(index).getPositionInGround(state))
        for index in range(bodies.getSize())
    }


def relax_pelvis_boundaries(model: osim.Model) -> None:
    """Widen ground-pelvis coordinate bounds for moving subjects.

    The stock Rajagopal model clamps pelvis translation to a few metres and
    rotation to ±90°. Fast locomotion (jogging, turns) drives markers past
    those limits; IK then freezes the skeleton while targets keep moving.
    """
    state = model.initSystem()
    coords = model.getCoordinateSet()
    wide = {
        "pelvis_tilt": (-math.pi, math.pi),
        "pelvis_list": (-math.pi, math.pi),
        "pelvis_rotation": (-math.pi, math.pi),
        "pelvis_tx": (-50.0, 50.0),
        "pelvis_ty": (-10.0, 10.0),
        "pelvis_tz": (-50.0, 50.0),
    }
    for name, (lo, hi) in wide.items():
        if not coords.contains(name):
            continue
        coord = coords.get(name)
        coord.setRangeMin(lo)
        coord.setRangeMax(hi)
        coord.setClamped(state, False)


def scaled_model(
    model_path: Path,
    scaled_path: Path,
    geometry_dirs: list[Path],
) -> osim.Model:
    for directory in geometry_dirs:
        osim.ModelVisualizer.addDirToGeometrySearchPaths(str(directory))

    model = osim.Model(str(model_path))
    relax_pelvis_boundaries(model)
    state = model.initSystem()
    scale_set = osim.ScaleSet()
    for index in range(model.getBodySet().getSize()):
        body_name = model.getBodySet().get(index).getName()
        scale = osim.Scale()
        scale.setSegmentName(body_name)
        scale.setScaleFactors(osim.Vec3(VISUAL_SCALE, VISUAL_SCALE, VISUAL_SCALE))
        scale_set.cloneAndAppend(scale)
    if not model.scale(state, scale_set, False, SUBJECT_MASS_KG):
        raise RuntimeError("OpenSim model scaling failed")
    model.setName("KineticLens_RajagopalLaiUhlrich2023_185cm_93kg")
    model.printToXML(str(scaled_path))
    return osim.Model(str(scaled_path))


def add_virtual_markers(model: osim.Model, output_path: Path) -> dict[str, np.ndarray]:
    state = model.initSystem()
    bodies = model.getBodySet()
    origins = body_origins(model, state)

    for marker_name, (parent_name, reference_name) in MARKER_SPECS.items():
        parent = bodies.get(parent_name)
        if parent_name == reference_name:
            local = osim.Vec3(0)
        else:
            # The model's default pelvis and torso frames are aligned with
            # ground, so the joint-center delta is already expressed in the
            # corresponding parent frame.
            local_array = origins[reference_name] - origins[parent_name]
            local = osim.Vec3(*[float(value) for value in local_array])
        model.addMarker(osim.Marker(marker_name, parent, local))

    model.finalizeConnections()
    model.initSystem()
    model.printToXML(str(output_path))
    return origins


def skeleton_dimensions(origins: dict[str, np.ndarray]) -> dict[str, float]:
    hip_l, hip_r = origins["femur_l"], origins["femur_r"]
    shoulder_l, shoulder_r = origins["humerus_l"], origins["humerus_r"]
    hip_mid = (hip_l + hip_r) * 0.5
    shoulder_mid = (shoulder_l + shoulder_r) * 0.5
    return {
        "hip_width": distance(hip_l, hip_r),
        "shoulder_width": distance(shoulder_l, shoulder_r),
        "torso": distance(hip_mid, shoulder_mid),
        "thigh_l": distance(hip_l, origins["tibia_l"]),
        "thigh_r": distance(hip_r, origins["tibia_r"]),
        "shank_l": distance(origins["tibia_l"], origins["talus_l"]),
        "shank_r": distance(origins["tibia_r"], origins["talus_r"]),
        "foot_l": distance(origins["talus_l"], origins["toes_l"]),
        "foot_r": distance(origins["talus_r"], origins["toes_r"]),
        "upper_arm_l": distance(shoulder_l, origins["ulna_l"]),
        "upper_arm_r": distance(shoulder_r, origins["ulna_r"]),
        "forearm_l": distance(origins["ulna_l"], origins["hand_l"]),
        "forearm_r": distance(origins["ulna_r"], origins["hand_r"]),
        "base_hip_y": float(hip_mid[1]),
    }


def smooth_window(time: float, start: float, peak: float, end: float) -> float:
    if time <= start or time >= end:
        return 0.0
    progress = (time - start) / (peak - start) if time <= peak else (end - time) / (end - peak)
    return math.sin(max(0.0, min(1.0, progress)) * math.pi * 0.5) ** 2


def fixed_segment_targets(
    pose3d: dict,
    dimensions: dict[str, float],
) -> tuple[list[float], list[dict[str, np.ndarray]]]:
    times: list[float] = []
    all_targets: list[dict[str, np.ndarray]] = []
    initial_hip_image_x = None

    for index, frame in enumerate(pose3d["frames"]):
        raw = mp_to_opensim(np.array(frame["world"], dtype=np.float64))
        image = np.array(frame["image"], dtype=np.float64)
        time = float(frame["time"])
        times.append(time)

        raw_hip_mid = (raw[23] + raw[24]) * 0.5
        raw_shoulder_mid = (raw[11] + raw[12]) * 0.5
        hip_image_x = float((image[23, 0] + image[24, 0]) * 0.5)
        if initial_hip_image_x is None:
            initial_hip_image_x = hip_image_x

        travel = (hip_image_x - initial_hip_image_x) * 2.55
        jump = 0.5 * smooth_window(time, 1.28, 1.72, 2.2)
        hip_mid = np.array([travel, dimensions["base_hip_y"] + jump, 0.0])

        hip_axis = unit(raw[24] - raw[23], np.array([0.0, 0.0, 1.0]))
        torso_axis = unit(raw_shoulder_mid - raw_hip_mid, np.array([0.0, 1.0, 0.0]))
        shoulder_axis_raw = raw[12] - raw[11]
        shoulder_axis = shoulder_axis_raw - torso_axis * float(np.dot(shoulder_axis_raw, torso_axis))
        shoulder_axis = unit(shoulder_axis, hip_axis)

        targets: dict[str, np.ndarray] = {}
        targets["L_HIP"] = hip_mid - hip_axis * dimensions["hip_width"] * 0.5
        targets["R_HIP"] = hip_mid + hip_axis * dimensions["hip_width"] * 0.5
        shoulder_mid = hip_mid + torso_axis * dimensions["torso"]
        targets["L_SHOULDER"] = shoulder_mid - shoulder_axis * dimensions["shoulder_width"] * 0.5
        targets["R_SHOULDER"] = shoulder_mid + shoulder_axis * dimensions["shoulder_width"] * 0.5

        for side, prefix in (("l", "L"), ("r", "R")):
            hip_i = 23 if side == "l" else 24
            knee_i = 25 if side == "l" else 26
            ankle_i = 27 if side == "l" else 28
            toe_i = 31 if side == "l" else 32
            shoulder_i = 11 if side == "l" else 12
            elbow_i = 13 if side == "l" else 14
            wrist_i = 15 if side == "l" else 16

            targets[f"{prefix}_KNEE"] = targets[f"{prefix}_HIP"] + unit(
                raw[knee_i] - raw[hip_i],
                np.array([0.0, -1.0, 0.0]),
            ) * dimensions[f"thigh_{side}"]
            targets[f"{prefix}_ANKLE"] = targets[f"{prefix}_KNEE"] + unit(
                raw[ankle_i] - raw[knee_i],
                np.array([0.0, -1.0, 0.0]),
            ) * dimensions[f"shank_{side}"]
            targets[f"{prefix}_TOE"] = targets[f"{prefix}_ANKLE"] + unit(
                raw[toe_i] - raw[ankle_i],
                np.array([1.0, 0.0, 0.0]),
            ) * dimensions[f"foot_{side}"]
            targets[f"{prefix}_ELBOW"] = targets[f"{prefix}_SHOULDER"] + unit(
                raw[elbow_i] - raw[shoulder_i],
                np.array([0.0, -1.0, 0.0]),
            ) * dimensions[f"upper_arm_{side}"]
            targets[f"{prefix}_WRIST"] = targets[f"{prefix}_ELBOW"] + unit(
                raw[wrist_i] - raw[elbow_i],
                np.array([0.0, -1.0, 0.0]),
            ) * dimensions[f"forearm_{side}"]

        all_targets.append(targets)

    return times, all_targets


def write_trc(path: Path, times: list[float], targets: list[dict[str, np.ndarray]], fps: float) -> None:
    names = list(MARKER_SPECS)
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
            values.extend(f"{value:.8f}" for value in frame[name])
        rows.append("\t".join(values))
    path.write_text("\n".join(rows) + "\n")


def run_ik(model_path: Path, marker_path: Path, motion_path: Path, times: list[float]) -> None:
    tool = osim.InverseKinematicsTool()
    tool.setName("KineticLens_Monocular3D_IK")
    model = osim.Model(str(model_path))
    tool.setModel(model)
    tool.setMarkerDataFileName(str(marker_path))
    tool.setStartTime(times[0])
    tool.setEndTime(times[-1])
    tool.setOutputMotionFileName(str(motion_path))
    tool.set_accuracy(1e-5)
    tool.set_report_errors(True)
    tool.set_report_marker_locations(False)
    task_set = tool.upd_IKTaskSet()
    for name, weight in MARKER_WEIGHTS.items():
        task = osim.IKMarkerTask()
        task.setName(name)
        task.setApply(True)
        task.setWeight(weight)
        task_set.cloneAndAppend(task)
    if not tool.run():
        raise RuntimeError("OpenSim inverse kinematics failed")


def read_motion(path: Path) -> tuple[list[str], np.ndarray, np.ndarray]:
    table = osim.TimeSeriesTable(str(path))
    labels = [str(label) for label in table.getColumnLabels()]
    times = np.array([float(table.getIndependentColumn()[i]) for i in range(table.getNumRows())])
    values = np.array(
        [
            np.array(table.getRowAtIndex(row).to_numpy(), dtype=np.float64)
            for row in range(table.getNumRows())
        ],
        dtype=np.float64,
    )
    return labels, times, values


def export_solved_motion(
    model_path: Path,
    motion_path: Path,
    output_path: Path,
    marker_targets: list[dict[str, np.ndarray]],
) -> None:
    model = osim.Model(str(model_path))
    state = model.initSystem()
    labels, times, values = read_motion(motion_path)
    coordinates = model.getCoordinateSet()
    bodies = model.getBodySet()
    muscles = model.getMuscles()
    markers = model.getMarkerSet()
    body_names = [bodies.get(index).getName() for index in range(bodies.getSize())]
    coordinate_lookup = {coordinates.get(index).getName(): coordinates.get(index) for index in range(coordinates.getSize())}

    frames = []
    error_rows = []
    for row, time in enumerate(times):
        for column, label in enumerate(labels):
            coordinate_name = label.rsplit("/", 2)[-2] if label.endswith("/value") else label
            coordinate = coordinate_lookup.get(coordinate_name)
            if coordinate is not None:
                value = float(values[row, column])
                if coordinate.getMotionType() == osim.Coordinate.Rotational:
                    value = math.radians(value)
                coordinate.setValue(state, value, False)
        model.realizePosition(state)

        body_transforms = {}
        for body_name in body_names:
            transform = bodies.get(body_name).getTransformInGround(state)
            body_transforms[body_name] = {
                "p": model_vec3(transform.p()),
                "q": rotation_to_quaternion(transform.R()),
            }

        muscle_paths = []
        for muscle_index in range(muscles.getSize()):
            muscle = muscles.get(muscle_index)
            path_points = muscle.getGeometryPath().getPathPointSet()
            points = []
            for point_index in range(path_points.getSize()):
                try:
                    points.append(model_vec3(path_points.get(point_index).getLocationInGround(state)))
                except Exception:
                    continue
            if len(points) >= 2:
                muscle_paths.append({"name": muscle.getName(), "points": points})

        marker_errors = []
        target_frame = marker_targets[min(row, len(marker_targets) - 1)]
        for marker_index in range(markers.getSize()):
            marker = markers.get(marker_index)
            name = marker.getName()
            if name not in target_frame:
                continue
            solved = as_array(marker.getLocationInGround(state))
            marker_errors.append(distance(solved, target_frame[name]))
        error_rows.append(marker_errors)

        solved_coordinates = {
            name: round(float(coordinate.getValue(state)), 8)
            for name, coordinate in coordinate_lookup.items()
        }
        pelvis = body_transforms["pelvis"]["p"]
        center_of_mass = model_vec3(model.calcMassCenterPosition(state))
        frames.append(
            {
                "time": round(float(time), 6),
                "pelvis": pelvis,
                "centerOfMass": center_of_mass,
                "bodies": body_transforms,
                "muscles": muscle_paths,
                "coordinates": solved_coordinates,
                "markerRmseM": round(float(np.sqrt(np.mean(np.square(marker_errors)))), 6),
            }
        )

    errors = np.array(error_rows)
    payload = {
        "metadata": {
            "modelName": model.getName(),
            "modelVersion": "OpenSim 4.6 / Rajagopal-Lai-Uhlrich 2023",
            "modelSource": "https://github.com/opensim-org/opensim-models",
            "modelLicense": "OpenSim official model repository; see bundled README and repository license",
            "bodyCount": bodies.getSize(),
            "muscleCount": muscles.getSize(),
            "subjectHeightCm": 185,
            "subjectMassKg": 93,
            "visualScale": round(VISUAL_SCALE, 6),
            "pipeline": "Monocular 3D landmarks → fixed-segment virtual markers → height scaling → OpenSim inverse kinematics",
            "solver": "OpenSim InverseKinematicsTool weighted marker least squares",
            "meanMarkerRmseM": round(float(np.mean(np.sqrt(np.mean(errors**2, axis=1)))), 6),
            "maxMarkerErrorM": round(float(np.max(errors)), 6),
            "warning": "Monocular 3D OpenSim IK preview — not calibrated optical motion capture, inverse dynamics, or clinical analysis",
        },
        "bodyNames": body_names,
        "frames": frames,
    }
    output_path.write_text(json.dumps(payload, separators=(",", ":")))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--pose3d", type=Path, required=True)
    parser.add_argument("--geometry", type=Path, action="append", required=True)
    parser.add_argument("--analysis-dir", type=Path, required=True)
    parser.add_argument("--public-model-dir", type=Path, required=True)
    args = parser.parse_args()

    args.analysis_dir.mkdir(parents=True, exist_ok=True)
    args.public_model_dir.mkdir(parents=True, exist_ok=True)
    scaled_path = args.analysis_dir / "KineticLens_185cm_93kg_scaled.osim"
    marked_path = args.analysis_dir / "KineticLens_185cm_93kg_markers.osim"
    trc_path = args.analysis_dir / "layup-monocular3d-markers.trc"
    mot_path = args.analysis_dir / "layup-opensim-ik.mot"

    model = scaled_model(args.model, scaled_path, args.geometry)
    origins = add_virtual_markers(model, marked_path)
    dimensions = skeleton_dimensions(origins)
    pose3d = json.loads(args.pose3d.read_text())
    times, targets = fixed_segment_targets(pose3d, dimensions)
    fps = float(pose3d["metadata"]["fps"])
    write_trc(trc_path, times, targets, fps)
    run_ik(marked_path, trc_path, mot_path, times)
    export_glb(marked_path, args.geometry, args.public_model_dir / "rajagopal-bones.glb")
    export_solved_motion(
        marked_path,
        mot_path,
        args.public_model_dir / "opensim-motion.json",
        targets,
    )

    metadata = json.loads((args.public_model_dir / "opensim-motion.json").read_text())["metadata"]
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
