#!/usr/bin/env python3
"""Estimate GRF from monocular OpenSim kinematics and run dynamics analyses.

This pipeline is intentionally labeled ESTIMATED GRF. It uses the whole-body
centre-of-mass acceleration, a video-derived flight window, and foot-height
contact weights. It is not a substitute for force-plate or pressure-insole
measurements.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import opensim as osim


GRAVITY_M_S2 = 9.80665
MASS_KG = 93.0
FILTER_WINDOW_FRAMES = 21
JOINTS = {
    "hip_r": {
        "joint": "hip_r",
        "body": "femur_r",
        "coordinate": "hip_flexion_r",
        "moment": "hip_flexion_r",
    },
    "knee_r": {
        "joint": "walker_knee_r",
        "body": "tibia_r",
        "coordinate": "knee_angle_r",
        "moment": "knee_angle_r",
    },
    "ankle_r": {
        "joint": "ankle_r",
        "body": "talus_r",
        "coordinate": "ankle_angle_r",
        "moment": "ankle_angle_r",
    },
    "hip_l": {
        "joint": "hip_l",
        "body": "femur_l",
        "coordinate": "hip_flexion_l",
        "moment": "hip_flexion_l",
    },
    "knee_l": {
        "joint": "walker_knee_l",
        "body": "tibia_l",
        "coordinate": "knee_angle_l",
        "moment": "knee_angle_l",
    },
    "ankle_l": {
        "joint": "ankle_l",
        "body": "talus_l",
        "coordinate": "ankle_angle_l",
        "moment": "ankle_angle_l",
    },
}


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def vec(values) -> list[float]:
    return [round(float(value), 6) for value in values]


def local_polynomial_kinematics(
    times: np.ndarray,
    positions: np.ndarray,
    window: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Cubic local-polynomial smoothing with analytic derivatives."""

    count = len(times)
    window = min(window, count if count % 2 == 1 else count - 1)
    half = window // 2
    smooth = np.zeros_like(positions)
    velocity = np.zeros_like(positions)
    acceleration = np.zeros_like(positions)

    for index, time in enumerate(times):
        start = max(0, index - half)
        end = min(count, index + half + 1)
        if end - start < window:
            if start == 0:
                end = min(count, window)
            else:
                start = max(0, count - window)
        local_time = times[start:end] - time
        for axis in range(positions.shape[1]):
            coefficients = np.polyfit(
                local_time,
                positions[start:end, axis],
                3,
            )
            smooth[index, axis] = coefficients[-1]
            velocity[index, axis] = coefficients[-2]
            acceleration[index, axis] = 2.0 * coefficients[-3]

    return smooth, velocity, acceleration


def write_storage(
    path: Path,
    labels: list[str],
    times: np.ndarray,
    values: np.ndarray,
    name: str,
) -> None:
    rows = [
        name,
        "version=1",
        f"nRows={len(times)}",
        f"nColumns={len(labels) + 1}",
        "inDegrees=no",
        "endheader",
        "\t".join(["time", *labels]),
    ]
    for time, row in zip(times, values):
        rows.append(
            "\t".join(
                [f"{time:.8f}", *[f"{float(value):.8f}" for value in row]]
            )
        )
    path.write_text("\n".join(rows) + "\n")


def write_external_loads(path: Path, data_path: Path) -> None:
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8" ?>
<OpenSimDocument Version="40000">
  <ExternalLoads name="KineticLens_ESTIMATED_GRF">
    <objects>
      <ExternalForce name="Estimated_Right_GRF">
        <applied_to_body>calcn_r</applied_to_body>
        <force_expressed_in_body>ground</force_expressed_in_body>
        <point_expressed_in_body>ground</point_expressed_in_body>
        <force_identifier>ground_force_r_v</force_identifier>
        <point_identifier>ground_force_r_p</point_identifier>
        <torque_identifier>ground_torque_r_</torque_identifier>
      </ExternalForce>
      <ExternalForce name="Estimated_Left_GRF">
        <applied_to_body>calcn_l</applied_to_body>
        <force_expressed_in_body>ground</force_expressed_in_body>
        <point_expressed_in_body>ground</point_expressed_in_body>
        <force_identifier>ground_force_l_v</force_identifier>
        <point_identifier>ground_force_l_p</point_identifier>
        <torque_identifier>ground_torque_l_</torque_identifier>
      </ExternalForce>
    </objects>
    <groups />
    <datafile>{data_path}</datafile>
    <lowpass_cutoff_frequency_for_load_kinematics>6</lowpass_cutoff_frequency_for_load_kinematics>
  </ExternalLoads>
</OpenSimDocument>
"""
    )


def create_dynamics_model(source_path: Path, output_path: Path) -> Path:
    """Create a torque-driven copy whose actuators match the ID output."""

    model = osim.Model(str(source_path.resolve()))
    model.setName("KineticLens_185cm_93kg_ESTIMATED_GRF")
    model.updForceSet().clearAndDestroy()
    coordinates = model.getCoordinateSet()
    for index in range(coordinates.getSize()):
        coordinate = coordinates.get(index)
        actuator = osim.CoordinateActuator()
        coordinate_name = coordinate.getName()
        actuator.setCoordinate(coordinate)
        suffix = (
            "_moment"
            if coordinate.getMotionType() == osim.Coordinate.Rotational
            else "_force"
        )
        actuator.setName(f"{coordinate_name}{suffix}")
        actuator.setOptimalForce(1.0)
        actuator.setMinControl(-1.0e6)
        actuator.setMaxControl(1.0e6)
        model.addForce(actuator)
    model.finalizeConnections()
    model.printToXML(str(output_path.resolve()))
    return output_path


def infer_flight_window(motion: dict) -> tuple[float, float]:
    """Infer airborne interval from foot clearance, or disable flight if none."""

    frames = motion["frames"]
    if not frames:
        return float("inf"), float("-inf")

    times = [float(frame["time"]) for frame in frames]
    lowest_foot = []
    for frame in frames:
        mins = []
        for side in ("l", "r"):
            calcaneus = frame["bodies"][f"calcn_{side}"]["p"]
            toes = frame["bodies"][f"toes_{side}"]["p"]
            mins.append(min(calcaneus[1], toes[1]))
        lowest_foot.append(min(mins))

    ground_level = float(np.percentile(lowest_foot, 5))
    clearance = np.array(lowest_foot, dtype=np.float64) - ground_level
    airborne = clearance > 0.06

    best_length = 0
    best_start = -1
    current_start = -1
    for index, is_airborne in enumerate(airborne):
        if is_airborne:
            if current_start < 0:
                current_start = index
        elif current_start >= 0:
            length = index - current_start
            if length > best_length:
                best_length = length
                best_start = current_start
            current_start = -1
    if current_start >= 0:
        length = len(airborne) - current_start
        if length > best_length:
            best_length = length
            best_start = current_start

    if best_start < 0 or best_length < 3:
        return float("inf"), float("-inf")

    best_end = best_start + best_length - 1
    return times[best_start], times[best_end]


def resolve_flight_window(
    motion: dict,
    events_path: Path | None,
) -> tuple[float, float]:
    if events_path and events_path.is_file():
        events = json.loads(events_path.read_text()).get("events", [])
        takeoff_time = next(
            (
                event["time"]
                for event in events
                if "toe-off" in event.get("label", "").lower()
            ),
            None,
        )
        landing_time = next(
            (
                event["time"]
                for event in events
                if "landing" in event.get("label", "").lower()
            ),
            None,
        )
        if takeoff_time is not None and landing_time is not None:
            return float(takeoff_time), float(landing_time)

    return infer_flight_window(motion)


def estimate_ground_reaction(
    motion: dict,
    takeoff_time: float,
    landing_time: float,
) -> tuple[np.ndarray, list[dict]]:
    frames = motion["frames"]
    times = np.array([frame["time"] for frame in frames], dtype=np.float64)
    centres = np.array(
        [frame["centerOfMass"] for frame in frames],
        dtype=np.float64,
    )
    _, _, com_acceleration = local_polynomial_kinematics(
        times,
        centres,
        FILTER_WINDOW_FRAMES,
    )

    foot_heights = {}
    foot_points = {}
    foot_vertical_velocity = {}
    for side in ("l", "r"):
        points = []
        heights = []
        for frame in frames:
            calcaneus = np.array(frame["bodies"][f"calcn_{side}"]["p"])
            toes = np.array(frame["bodies"][f"toes_{side}"]["p"])
            points.append((calcaneus + toes) * 0.5)
            heights.append(min(calcaneus[1], toes[1]))
        foot_points[side] = np.array(points)
        foot_heights[side] = np.array(heights)
        _, vertical_velocity, _ = local_polynomial_kinematics(
            times,
            np.array(heights)[:, None],
            11,
        )
        foot_vertical_velocity[side] = vertical_velocity[:, 0]

    minimum_height = {
        side: float(np.percentile(foot_heights[side], 5))
        for side in ("l", "r")
    }
    body_weight_n = MASS_KG * GRAVITY_M_S2
    gravity = np.array([0.0, -GRAVITY_M_S2, 0.0])
    raw_force = MASS_KG * (com_acceleration - gravity)
    output_rows = []
    details = []

    for index, time in enumerate(times):
        flight = takeoff_time <= time < landing_time
        scores = {}
        for side in ("l", "r"):
            clearance = max(
                0.0,
                foot_heights[side][index] - minimum_height[side],
            )
            height_score = math.exp(-0.5 * (clearance / 0.085) ** 2)
            speed_score = math.exp(
                -0.5 * (foot_vertical_velocity[side][index] / 1.15) ** 2
            )
            scores[side] = 0.0 if flight else height_score * speed_score

        score_total = scores["l"] + scores["r"]
        if not flight and score_total < 1e-5:
            lower_side = min(("l", "r"), key=lambda side: foot_heights[side][index])
            scores[lower_side] = 1.0
            score_total = 1.0
        weights = {
            side: (scores[side] / score_total if score_total > 0 else 0.0)
            for side in ("l", "r")
        }

        total_force = raw_force[index].copy()
        if flight:
            total_force[:] = 0.0
        else:
            total_force[1] = clamp(
                float(total_force[1]),
                0.15 * body_weight_n,
                3.0 * body_weight_n,
            )
            horizontal = total_force[[0, 2]]
            horizontal_magnitude = float(np.linalg.norm(horizontal))
            horizontal_limit = 0.8 * total_force[1]
            if horizontal_magnitude > horizontal_limit > 0:
                total_force[[0, 2]] *= horizontal_limit / horizontal_magnitude

        side_forces = {
            side: total_force * weights[side]
            for side in ("l", "r")
        }
        side_points = {
            side: np.array(
                [
                    foot_points[side][index, 0],
                    0.0,
                    foot_points[side][index, 2],
                ]
            )
            for side in ("l", "r")
        }

        row = []
        for side in ("r", "l"):
            row.extend(side_forces[side])
            row.extend(side_points[side])
            row.extend([0.0, 0.0, 0.0])
        output_rows.append(row)

        fit_confidence = math.exp(
            -float(frames[index]["markerRmseM"]) / 0.055
        )
        contact_confidence = clamp(max(scores.values()), 0.0, 1.0)
        confidence = clamp(
            0.23 + 0.22 * fit_confidence + 0.16 * contact_confidence,
            0.2,
            0.61,
        )
        if flight:
            phase = "flight"
        elif weights["l"] > 0.2 and weights["r"] > 0.2:
            phase = "double support"
        elif weights["l"] >= weights["r"]:
            phase = "left support"
        else:
            phase = "right support"

        details.append(
            {
                "phase": phase,
                "confidence": round(confidence, 4),
                "total": {
                    "force": vec(total_force),
                    "magnitudeN": round(float(np.linalg.norm(total_force)), 3),
                    "bodyWeight": round(
                        float(np.linalg.norm(total_force)) / body_weight_n,
                        4,
                    ),
                },
                "left": {
                    "active": weights["l"] > 0.08,
                    "weight": round(weights["l"], 4),
                    "force": vec(side_forces["l"]),
                    "point": vec(side_points["l"]),
                },
                "right": {
                    "active": weights["r"] > 0.08,
                    "weight": round(weights["r"], 4),
                    "force": vec(side_forces["r"]),
                    "point": vec(side_points["r"]),
                },
            }
        )

    return np.array(output_rows), details


def storage_to_arrays(path: Path) -> tuple[list[str], np.ndarray, np.ndarray]:
    table = osim.TimeSeriesTable(str(path))
    labels = [str(label) for label in table.getColumnLabels()]
    times = np.array(
        [
            float(table.getIndependentColumn()[index])
            for index in range(table.getNumRows())
        ]
    )
    values = np.array(
        [
            np.array(table.getRowAtIndex(row).to_numpy(), dtype=np.float64)
            for row in range(table.getNumRows())
        ]
    )
    return labels, times, values


def run_inverse_dynamics(
    model_path: Path,
    motion_path: Path,
    external_loads_path: Path,
    results_dir: Path,
    start_time: float,
    end_time: float,
) -> Path:
    output_name = "estimated_inverse_dynamics.sto"
    setup_path = results_dir / "setup-estimated-inverse-dynamics.xml"
    tool = osim.InverseDynamicsTool()
    tool.setName("KineticLens_ESTIMATED_GRF_InverseDynamics")
    tool.setModelFileName(str(model_path.resolve()))
    tool.setCoordinatesFileName(str(motion_path.resolve()))
    tool.setExternalLoadsFileName(str(external_loads_path.resolve()))
    tool.setStartTime(start_time)
    tool.setEndTime(end_time)
    tool.setLowpassCutoffFrequency(6.0)
    tool.setResultsDir(str(results_dir.resolve()))
    tool.setOutputGenForceFileName(output_name)
    tool.printToXML(str(setup_path))
    configured_tool = osim.InverseDynamicsTool(str(setup_path))
    if not configured_tool.run():
        raise RuntimeError("OpenSim InverseDynamicsTool failed")
    return results_dir / output_name


def array_str(values: list[str]) -> osim.ArrayStr:
    output = osim.ArrayStr()
    for value in values:
        output.append(value)
    return output


def run_joint_reaction(
    model_path: Path,
    motion_path: Path,
    external_loads_path: Path,
    inverse_dynamics_path: Path,
    results_dir: Path,
    start_time: float,
    end_time: float,
) -> Path:
    analysis = osim.JointReaction()
    analysis.setName("SelectedJointReaction")
    analysis.setForcesFileName(str(inverse_dynamics_path.resolve()))
    analysis.setJointNames(
        array_str([spec["joint"] for spec in JOINTS.values()])
    )
    analysis.setOnBody(array_str(["child"] * len(JOINTS)))
    analysis.setInFrame(array_str(["ground"] * len(JOINTS)))

    setup_path = results_dir / "setup-estimated-joint-reaction.xml"
    tool = osim.AnalyzeTool()
    tool.setName("KineticLens_ESTIMATED_GRF")
    tool.setModelFilename(str(model_path.resolve()))
    tool.setCoordinatesFileName(str(motion_path.resolve()))
    tool.setExternalLoadsFileName(str(external_loads_path.resolve()))
    tool.setStartTime(start_time)
    tool.setFinalTime(end_time)
    tool.setLowpassCutoffFrequency(6.0)
    tool.setResultsDir(str(results_dir.resolve()))
    tool.updAnalysisSet().cloneAndAppend(analysis)
    tool.printToXML(str(setup_path))
    configured_tool = osim.AnalyzeTool(str(setup_path))
    if not configured_tool.run():
        raise RuntimeError("OpenSim JointReaction analysis failed")

    candidates = sorted(results_dir.glob("*ReactionLoads*.sto"))
    if not candidates:
        raise RuntimeError("JointReaction output was not created")
    return candidates[-1]


def nearest_row(source_times: np.ndarray, time: float) -> int:
    return int(np.argmin(np.abs(source_times - time)))


def match_column(labels: list[str], candidates: list[str]) -> int | None:
    for candidate in candidates:
        if candidate in labels:
            return labels.index(candidate)
    for index, label in enumerate(labels):
        if any(candidate in label for candidate in candidates):
            return index
    return None


def vector_from_columns(
    labels: list[str],
    values: np.ndarray,
    row: int,
    prefix: str,
    suffixes: tuple[str, str, str],
) -> np.ndarray:
    output = []
    for suffix in suffixes:
        index = match_column(labels, [f"{prefix}{suffix}"])
        output.append(values[row, index] if index is not None else 0.0)
    return np.array(output, dtype=np.float64)


def export_dynamics(
    motion: dict,
    grf_details: list[dict],
    inverse_dynamics_path: Path,
    reaction_path: Path,
    output_path: Path,
) -> None:
    id_labels, id_times, id_values = storage_to_arrays(inverse_dynamics_path)
    reaction_labels, reaction_times, reaction_values = storage_to_arrays(
        reaction_path
    )
    times = np.array([frame["time"] for frame in motion["frames"]])
    coordinate_series = {
        joint_id: np.array(
            [
                frame["coordinates"].get(spec["coordinate"], 0.0)
                for frame in motion["frames"]
            ]
        )
        for joint_id, spec in JOINTS.items()
    }
    angular_velocity = {
        joint_id: np.gradient(series, times)
        for joint_id, series in coordinate_series.items()
    }
    body_weight_n = MASS_KG * GRAVITY_M_S2
    frames = []

    for frame_index, (frame, grf) in enumerate(
        zip(motion["frames"], grf_details)
    ):
        time = float(frame["time"])
        id_row = nearest_row(id_times, time)
        reaction_row = nearest_row(reaction_times, time)
        joints = {}
        for joint_id, spec in JOINTS.items():
            reaction_prefix = (
                f"{spec['joint']}_on_{spec['body']}_in_ground_"
            )
            force = vector_from_columns(
                reaction_labels,
                reaction_values,
                reaction_row,
                reaction_prefix,
                ("fx", "fy", "fz"),
            )
            reaction_moment = vector_from_columns(
                reaction_labels,
                reaction_values,
                reaction_row,
                reaction_prefix,
                ("mx", "my", "mz"),
            )
            moment_index = match_column(
                id_labels,
                [
                    f"{spec['moment']}_moment",
                    spec["moment"],
                ],
            )
            flexion_moment = (
                float(id_values[id_row, moment_index])
                if moment_index is not None
                else 0.0
            )
            force_magnitude = float(np.linalg.norm(force))
            joints[joint_id] = {
                "position": frame["bodies"][spec["body"]]["p"],
                "force": vec(force),
                "magnitudeN": round(force_magnitude, 3),
                "bodyWeight": round(force_magnitude / body_weight_n, 4),
                "reactionMoment": vec(reaction_moment),
                "momentMagnitudeNm": round(
                    float(np.linalg.norm(reaction_moment)),
                    3,
                ),
                "flexionMomentNm": round(flexion_moment, 3),
                "angleRad": round(
                    float(coordinate_series[joint_id][frame_index]),
                    8,
                ),
                "angularVelocityRadS": round(
                    float(angular_velocity[joint_id][frame_index]),
                    6,
                ),
                "confidence": grf["confidence"],
            }
        frames.append(
            {
                "time": round(time, 6),
                "contact": {
                    "phase": grf["phase"],
                    "left": grf["left"]["active"],
                    "right": grf["right"]["active"],
                },
                "grf": {
                    "total": grf["total"],
                    "left": grf["left"],
                    "right": grf["right"],
                },
                "joints": joints,
            }
        )

    payload = {
        "metadata": {
            "status": "ESTIMATED GRF",
            "subjectMassKg": 93,
            "bodyWeightN": round(body_weight_n, 3),
            "source": "OpenSim whole-body COM acceleration + video-estimated foot contact",
            "solver": "OpenSim InverseDynamicsTool + JointReaction",
            "comFilter": f"local cubic polynomial, {FILTER_WINDOW_FRAMES} frames",
            "kinematicsLowpassHz": 6,
            "confidenceCeiling": 0.61,
            "warning": "Estimated from monocular video without force plates, pressure insoles, calibrated cameras, or measured centre of pressure. Not valid for clinical or performance decisions.",
        },
        "jointIds": list(JOINTS),
        "frames": frames,
    }
    output_path.write_text(json.dumps(payload, separators=(",", ":")))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--motion", type=Path, required=True)
    parser.add_argument("--motion-json", type=Path, required=True)
    parser.add_argument("--events-json", type=Path)
    parser.add_argument("--analysis-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    args.analysis_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    motion = json.loads(args.motion_json.read_text())
    takeoff_time, landing_time = resolve_flight_window(motion, args.events_json)
    times = np.array([frame["time"] for frame in motion["frames"]])
    grf_values, grf_details = estimate_ground_reaction(
        motion,
        takeoff_time,
        landing_time,
    )

    grf_labels = []
    for side in ("r", "l"):
        grf_labels.extend(
            [
                f"ground_force_{side}_vx",
                f"ground_force_{side}_vy",
                f"ground_force_{side}_vz",
                f"ground_force_{side}_px",
                f"ground_force_{side}_py",
                f"ground_force_{side}_pz",
                f"ground_torque_{side}_x",
                f"ground_torque_{side}_y",
                f"ground_torque_{side}_z",
            ]
        )
    grf_path = args.analysis_dir / "estimated-grf.sto"
    external_loads_path = args.analysis_dir / "estimated-external-loads.xml"
    write_storage(
        grf_path,
        grf_labels,
        times,
        grf_values,
        "KineticLens_ESTIMATED_GRF",
    )
    write_external_loads(external_loads_path, grf_path.resolve())
    dynamics_model_path = create_dynamics_model(
        args.model,
        args.analysis_dir / "KineticLens_185cm_93kg_dynamics.osim",
    )
    inverse_dynamics_path = run_inverse_dynamics(
        dynamics_model_path,
        args.motion,
        external_loads_path,
        args.analysis_dir,
        float(times[0]),
        float(times[-1]),
    )
    reaction_path = run_joint_reaction(
        dynamics_model_path,
        args.motion,
        external_loads_path,
        inverse_dynamics_path,
        args.analysis_dir,
        float(times[0]),
        float(times[-1]),
    )
    export_dynamics(
        motion,
        grf_details,
        inverse_dynamics_path,
        reaction_path,
        args.output,
    )
    print(
        json.dumps(
            {
                "grf": str(grf_path),
                "inverseDynamics": str(inverse_dynamics_path),
                "jointReaction": str(reaction_path),
                "output": str(args.output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
