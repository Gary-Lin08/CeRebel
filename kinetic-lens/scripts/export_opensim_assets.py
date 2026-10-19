#!/usr/bin/env python3
"""Export official OpenSim geometry and frame transforms for Kinetic Lens.

This script intentionally depends only on the OpenSim Python bindings and the
standard library. The VTP meshes in the official OpenSim model repository are
ASCII PolyData files, so they can be converted to a compact GLB without VTK.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import xml.etree.ElementTree as ET
from array import array
from pathlib import Path
from typing import Iterable

import opensim as osim


SUBJECT_HEIGHT_M = 1.85
SUBJECT_MASS_KG = 93.0
GENERIC_VISUAL_HEIGHT_M = 1.75
VISUAL_SCALE = SUBJECT_HEIGHT_M / GENERIC_VISUAL_HEIGHT_M


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def vec3(value) -> list[float]:
    return [round(float(value.get(i)) * VISUAL_SCALE, 6) for i in range(3)]


def rotation_to_quaternion(rotation) -> list[float]:
    m = [[float(rotation.get(i, j)) for j in range(3)] for i in range(3)]
    trace = m[0][0] + m[1][1] + m[2][2]

    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        w = 0.25 * s
        x = (m[2][1] - m[1][2]) / s
        y = (m[0][2] - m[2][0]) / s
        z = (m[1][0] - m[0][1]) / s
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2
        w = (m[2][1] - m[1][2]) / s
        x = 0.25 * s
        y = (m[0][1] + m[1][0]) / s
        z = (m[0][2] + m[2][0]) / s
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2
        w = (m[0][2] - m[2][0]) / s
        x = (m[0][1] + m[1][0]) / s
        y = 0.25 * s
        z = (m[1][2] + m[2][1]) / s
    else:
        s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2
        w = (m[1][0] - m[0][1]) / s
        x = (m[0][2] + m[2][0]) / s
        y = (m[1][2] + m[2][1]) / s
        z = 0.25 * s

    return [round(x, 7), round(y, 7), round(z, 7), round(w, 7)]


def angle_between(a, b, c) -> float:
    ab = (a[0] - b[0], a[1] - b[1])
    cb = (c[0] - b[0], c[1] - b[1])
    denom = math.hypot(*ab) * math.hypot(*cb)
    if denom < 1e-9:
        return 180.0
    cosine = clamp((ab[0] * cb[0] + ab[1] * cb[1]) / denom, -1.0, 1.0)
    return math.degrees(math.acos(cosine))


def arm_elevation(shoulder, elbow) -> float:
    dx = elbow[0] - shoulder[0]
    dy = elbow[1] - shoulder[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return 0.0
    return math.degrees(math.acos(clamp(dy / length, -1.0, 1.0)))


def smooth_window(time: float, start: float, peak: float, end: float) -> float:
    if time <= start or time >= end:
        return 0.0
    if time <= peak:
        progress = (time - start) / (peak - start)
    else:
        progress = (end - time) / (end - peak)
    return math.sin(progress * math.pi * 0.5) ** 2


def coordinate_values(pose_frame: dict, index: int, fps: float, defaults: dict[str, float]):
    keypoints = pose_frame["keypoints"]
    angles = pose_frame["angles"]
    time = index / fps

    left_shoulder, right_shoulder = keypoints[5], keypoints[6]
    left_elbow, right_elbow = keypoints[7], keypoints[8]
    left_wrist, right_wrist = keypoints[9], keypoints[10]

    left_elbow_flex = 180.0 - angle_between(left_shoulder, left_elbow, left_wrist)
    right_elbow_flex = 180.0 - angle_between(right_shoulder, right_elbow, right_wrist)

    jump = 0.54 * smooth_window(time, 1.34, 1.76, 2.18)
    takeoff = smooth_window(time, 1.1, 1.45, 1.8)
    hip_center_x = (keypoints[11][0] + keypoints[12][0]) * 0.5
    travel = max(0.0, hip_center_x - 0.265) * 3.15

    values = dict(defaults)
    values.update(
        {
            "pelvis_tx": travel / VISUAL_SCALE,
            "pelvis_ty": defaults.get("pelvis_ty", 0.94) + jump / VISUAL_SCALE,
            "pelvis_tilt": math.radians(clamp((angles["trunkLean"] - 18.0) * 0.15, -8, 8)),
            "pelvis_list": math.radians(clamp((keypoints[11][1] - keypoints[12][1]) * 120, -7, 7)),
            "pelvis_rotation": math.radians(clamp((hip_center_x - 0.5) * 8, -5, 5)),
            "hip_flexion_l": math.radians(clamp(180.0 - angles["leftHip"], -30, 110)),
            "hip_flexion_r": math.radians(clamp(180.0 - angles["rightHip"], -30, 110)),
            "knee_angle_l": math.radians(clamp(180.0 - angles["leftKnee"], 0, 130)),
            "knee_angle_r": math.radians(clamp(180.0 - angles["rightKnee"], 0, 130)),
            "ankle_angle_l": math.radians(-8 - 18 * takeoff),
            "ankle_angle_r": math.radians(-8 - 18 * takeoff),
            "lumbar_extension": math.radians(clamp(-(angles["trunkLean"] - 14.0) * 0.72, -28, 12)),
            "arm_flex_l": math.radians(clamp(arm_elevation(left_shoulder, left_elbow), -20, 155)),
            "arm_flex_r": math.radians(clamp(arm_elevation(right_shoulder, right_elbow), -20, 155)),
            "arm_add_l": math.radians(-8),
            "arm_add_r": math.radians(8),
            "elbow_flex_l": math.radians(clamp(left_elbow_flex, 0, 135)),
            "elbow_flex_r": math.radians(clamp(right_elbow_flex, 0, 135)),
        }
    )
    return values


def parse_ascii_vtp(path: Path):
    root = ET.parse(path).getroot()
    piece = root.find(".//Piece")
    if piece is None:
        raise ValueError(f"No PolyData Piece in {path}")

    points_array = piece.find("./Points/DataArray")
    if points_array is None or points_array.text is None:
        raise ValueError(f"No point array in {path}")
    raw_points = [float(value) * VISUAL_SCALE for value in points_array.text.split()]
    vertices = [raw_points[index : index + 3] for index in range(0, len(raw_points), 3)]

    connectivity_array = piece.find("./Polys/DataArray[@Name='connectivity']")
    offsets_array = piece.find("./Polys/DataArray[@Name='offsets']")
    if connectivity_array is None or offsets_array is None:
        arrays = piece.findall("./Polys/DataArray")
        connectivity_array, offsets_array = arrays[0], arrays[1]
    connectivity = [int(value) for value in (connectivity_array.text or "").split()]
    offsets = [int(value) for value in (offsets_array.text or "").split()]

    faces: list[list[int]] = []
    start = 0
    for offset in offsets:
        polygon = connectivity[start:offset]
        for index in range(1, len(polygon) - 1):
            faces.append([polygon[0], polygon[index], polygon[index + 1]])
        start = offset

    return vertices, faces


def calculate_normals(vertices: list[list[float]], faces: list[list[int]]):
    normals = [[0.0, 0.0, 0.0] for _ in vertices]
    for a, b, c in faces:
        va, vb, vc = vertices[a], vertices[b], vertices[c]
        ab = [vb[i] - va[i] for i in range(3)]
        ac = [vc[i] - va[i] for i in range(3)]
        normal = [
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        ]
        for vertex_index in (a, b, c):
            for axis in range(3):
                normals[vertex_index][axis] += normal[axis]

    for normal in normals:
        length = math.sqrt(sum(value * value for value in normal)) or 1.0
        for axis in range(3):
            normal[axis] /= length
    return normals


def mesh_manifest(model_path: Path):
    root = ET.parse(model_path).getroot()
    bodies: dict[str, list[str]] = {}
    for body in root.findall(".//BodySet/objects/Body"):
        name = body.attrib["name"]
        meshes = [
            mesh_file.text.strip()
            for mesh_file in body.findall("./attached_geometry/Mesh/mesh_file")
            if mesh_file.text
        ]
        if meshes:
            bodies[name] = meshes
    return bodies


def resolve_mesh(name: str, geometry_dirs: Iterable[Path]) -> Path:
    for directory in geometry_dirs:
        candidate = directory / name
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Official mesh not found: {name}")


def pad4(blob: bytes, byte: bytes = b"\x00") -> bytes:
    return blob + byte * ((4 - len(blob) % 4) % 4)


def export_glb(model_path: Path, geometry_dirs: list[Path], output_path: Path):
    manifest = mesh_manifest(model_path)
    gltf = {
        "asset": {"version": "2.0", "generator": "Kinetic Lens OpenSim exporter"},
        "scene": 0,
        "scenes": [{"nodes": []}],
        "nodes": [],
        "meshes": [],
        "accessors": [],
        "bufferViews": [],
        "buffers": [{"byteLength": 0}],
        "materials": [
            {
                "name": "OpenSim bone",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.93, 0.91, 0.86, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.64,
                },
            }
        ],
    }
    binary = bytearray()

    def append_buffer(data: bytes, target: int) -> int:
        nonlocal binary
        while len(binary) % 4:
            binary.append(0)
        offset = len(binary)
        binary.extend(data)
        index = len(gltf["bufferViews"])
        gltf["bufferViews"].append(
            {
                "buffer": 0,
                "byteOffset": offset,
                "byteLength": len(data),
                "target": target,
            }
        )
        return index

    for body_name, mesh_names in manifest.items():
        vertices: list[list[float]] = []
        faces: list[list[int]] = []
        for mesh_name in mesh_names:
            mesh_vertices, mesh_faces = parse_ascii_vtp(resolve_mesh(mesh_name, geometry_dirs))
            vertex_offset = len(vertices)
            vertices.extend(mesh_vertices)
            faces.extend([[index + vertex_offset for index in face] for face in mesh_faces])

        normals = calculate_normals(vertices, faces)
        flat_vertices = array("f", [value for vertex in vertices for value in vertex])
        flat_normals = array("f", [value for normal in normals for value in normal])
        flat_indices = array("I", [value for face in faces for value in face])

        position_view = append_buffer(flat_vertices.tobytes(), 34962)
        normal_view = append_buffer(flat_normals.tobytes(), 34962)
        index_view = append_buffer(flat_indices.tobytes(), 34963)

        mins = [min(vertex[axis] for vertex in vertices) for axis in range(3)]
        maxs = [max(vertex[axis] for vertex in vertices) for axis in range(3)]
        position_accessor = len(gltf["accessors"])
        gltf["accessors"].append(
            {
                "bufferView": position_view,
                "componentType": 5126,
                "count": len(vertices),
                "type": "VEC3",
                "min": mins,
                "max": maxs,
            }
        )
        normal_accessor = len(gltf["accessors"])
        gltf["accessors"].append(
            {
                "bufferView": normal_view,
                "componentType": 5126,
                "count": len(normals),
                "type": "VEC3",
            }
        )
        index_accessor = len(gltf["accessors"])
        gltf["accessors"].append(
            {
                "bufferView": index_view,
                "componentType": 5125,
                "count": len(flat_indices),
                "type": "SCALAR",
            }
        )

        mesh_index = len(gltf["meshes"])
        gltf["meshes"].append(
            {
                "name": body_name,
                "primitives": [
                    {
                        "attributes": {
                            "POSITION": position_accessor,
                            "NORMAL": normal_accessor,
                        },
                        "indices": index_accessor,
                        "material": 0,
                    }
                ],
            }
        )
        node_index = len(gltf["nodes"])
        gltf["nodes"].append({"name": body_name, "mesh": mesh_index})
        gltf["scenes"][0]["nodes"].append(node_index)

    gltf["buffers"][0]["byteLength"] = len(binary)
    json_chunk = pad4(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" ")
    bin_chunk = pad4(bytes(binary))
    total_length = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)
    header = struct.pack("<III", 0x46546C67, 2, total_length)
    output_path.write_bytes(
        header
        + struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(bin_chunk), 0x004E4942)
        + bin_chunk
    )


def set_coordinate(model, state, name: str, value: float):
    coordinates = model.getCoordinateSet()
    try:
        coordinate = coordinates.get(name)
    except Exception:
        return
    low = float(coordinate.getRangeMin())
    high = float(coordinate.getRangeMax())
    coordinate.setValue(state, clamp(value, low, high), False)


def export_motion(model_path: Path, pose_path: Path, output_path: Path, geometry_dirs: list[Path]):
    for directory in geometry_dirs:
        osim.ModelVisualizer.addDirToGeometrySearchPaths(str(directory))

    model = osim.Model(str(model_path))
    state = model.initSystem()
    coordinate_set = model.getCoordinateSet()
    defaults = {
        coordinate_set.get(index).getName(): float(coordinate_set.get(index).getValue(state))
        for index in range(coordinate_set.getSize())
    }
    body_set = model.getBodySet()
    muscles = model.getMuscles()
    body_names = [body_set.get(index).getName() for index in range(body_set.getSize())]
    default_mass = sum(float(body_set.get(index).getMass()) for index in range(body_set.getSize()))

    pose = json.loads(pose_path.read_text())
    fps = float(pose["metadata"]["nominalFps"])
    frames = []

    for index, pose_frame in enumerate(pose["frames"]):
        values = coordinate_values(pose_frame, index, fps, defaults)
        for name, value in values.items():
            set_coordinate(model, state, name, value)
        model.realizePosition(state)

        bodies = {}
        for body_name in body_names:
            transform = body_set.get(body_name).getTransformInGround(state)
            bodies[body_name] = {
                "p": vec3(transform.p()),
                "q": rotation_to_quaternion(transform.R()),
            }

        muscle_paths = []
        for muscle_index in range(muscles.getSize()):
            muscle = muscles.get(muscle_index)
            path_points = muscle.getGeometryPath().getPathPointSet()
            points = []
            for point_index in range(path_points.getSize()):
                point = path_points.get(point_index)
                try:
                    points.append(vec3(point.getLocationInGround(state)))
                except Exception:
                    continue
            if len(points) >= 2:
                muscle_paths.append({"name": muscle.getName(), "points": points})

        pelvis = bodies["pelvis"]["p"]
        frames.append(
            {
                "time": round(index / fps, 5),
                "pelvis": pelvis,
                "bodies": bodies,
                "muscles": muscle_paths,
            }
        )

    payload = {
        "metadata": {
            "modelName": model.getName(),
            "modelVersion": "OpenSim 4.6 / Rajagopal-Lai-Uhlrich 2023",
            "modelSource": "https://github.com/opensim-org/opensim-models",
            "modelLicense": "OpenSim official model repository; see bundled README and repository license",
            "bodyCount": body_set.getSize(),
            "muscleCount": muscles.getSize(),
            "defaultModelMassKg": round(default_mass, 3),
            "subjectHeightCm": 185,
            "subjectMassKg": 93,
            "visualScale": round(VISUAL_SCALE, 6),
            "pipeline": "Single-view 2D pose → apparent angles → OpenSim coordinate retargeting",
            "warning": "Kinematic preview only — not marker-based IK, inverse dynamics, or clinical analysis",
        },
        "bodyNames": body_names,
        "frames": frames,
    }
    output_path.write_text(json.dumps(payload, separators=(",", ":")))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--pose", type=Path, required=True)
    parser.add_argument("--geometry", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    export_glb(args.model, args.geometry, args.output_dir / "rajagopal-bones.glb")
    export_motion(
        args.model,
        args.pose,
        args.output_dir / "opensim-motion.json",
        args.geometry,
    )


if __name__ == "__main__":
    main()
