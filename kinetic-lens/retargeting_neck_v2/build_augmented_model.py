"""Build RajagopalLaiUhlrich2023_neck.osim with torso→neck→head (3+3 rotational DOFs)."""

from __future__ import annotations

import argparse
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from .config import (
    AUGMENTED_OSIM,
    CERVICAL_RANGE_RAD,
    GEOMETRY_DIR,
    HEAD_COM,
    HEAD_IN_NECK,
    HEAD_IN_TORSO,
    HEAD_INERTIA,
    HEAD_MASS_KG,
    LOCAL_GEOMETRY_DIR,
    NECK_COM,
    NECK_IN_TORSO,
    NECK_INERTIA,
    NECK_MASS_KG,
    RAJAGOPAL_OSIM,
    TORSO_COM,
    TORSO_INERTIA,
    TORSO_MASS_KG,
)


def _parse_vtp_points(path: Path) -> tuple[list[list[float]], str, str, str]:
    tree = ET.parse(path)
    root = tree.getroot()
    piece = root.find(".//Piece")
    assert piece is not None
    points = piece.find("./Points/DataArray")
    assert points is not None and points.text is not None
    conn = piece.find("./Polys/DataArray[@Name='connectivity']")
    offs = piece.find("./Polys/DataArray[@Name='offsets']")
    if conn is None or offs is None:
        arrays = piece.findall("./Polys/DataArray")
        conn, offs = arrays[0], arrays[1]
    values = [float(v) for v in points.text.split()]
    verts = [values[i : i + 3] for i in range(0, len(values), 3)]
    return verts, points.text, conn.text or "", offs.text or ""


def _write_vtp(path: Path, vertices: list[list[float]], connectivity: str, offsets: str) -> None:
    n = len(vertices)
    n_polys = len(offsets.split())
    coords = "\n\t\t\t\t" + " ".join(f"{c:.8g}" for v in vertices for c in v) + "\n"
    path.write_text(
        f"""<?xml version="1.0"?>
<VTKFile type="PolyData" version="0.1">
  <PolyData>
    <Piece NumberOfPoints="{n}" NumberOfPolys="{n_polys}">
      <Points>
        <DataArray type="Float32" NumberOfComponents="3" format="ascii">{coords}</DataArray>
      </Points>
      <Polys>
        <DataArray type="Int32" Name="connectivity" format="ascii">{connectivity}</DataArray>
        <DataArray type="Int32" Name="offsets" format="ascii">{offsets}</DataArray>
      </Polys>
    </Piece>
  </PolyData>
</VTKFile>
"""
    )


def _shifted_official_mesh(src_name: str, dest_name: str, shift: tuple[float, float, float]) -> None:
    verts, _, conn, offs = _parse_vtp_points(GEOMETRY_DIR / src_name)
    moved = [[p[0] - shift[0], p[1] - shift[1], p[2] - shift[2]] for p in verts]
    _write_vtp(LOCAL_GEOMETRY_DIR / dest_name, moved, conn, offs)


def _neck_cylinder_vtp(path: Path, length: float = 0.09, radius: float = 0.028, segs: int = 12) -> None:
    verts: list[list[float]] = []
    faces: list[list[int]] = []
    for ring, y in enumerate((0.0, length)):
        for i in range(segs):
            ang = 2 * math.pi * i / segs
            verts.append([radius * math.sin(ang), y, radius * math.cos(ang)])
    for i in range(segs):
        a, b = i, (i + 1) % segs
        c, d = i + segs, (i + 1) % segs + segs
        faces.append([a, b, d])
        faces.append([a, d, c])
    conn = " ".join(str(i) for f in faces for i in f)
    offs = " ".join(str((k + 1) * 3) for k in range(len(faces)))
    _write_vtp(path, verts, conn, offs)


def _mesh_xml(name: str, mesh_file: str, indent: str = "\t\t\t\t\t") -> str:
    return f"""{indent}<Mesh name="{name}">
{indent}	<socket_frame>..</socket_frame>
{indent}	<scale_factors>1 1 1</scale_factors>
{indent}	<Appearance>
{indent}		<opacity>1</opacity>
{indent}		<color>1 1 1</color>
{indent}	</Appearance>
{indent}	<mesh_file>{mesh_file}</mesh_file>
{indent}</Mesh>
"""


def _body_xml(name: str, mass: float, com: tuple, inertia: tuple, meshes: str) -> str:
    cx, cy, cz = com
    ixx, iyy, izz, ixy, ixz, iyz = inertia
    return f"""				<Body name="{name}">
					<FrameGeometry name="frame_geometry">
						<socket_frame>..</socket_frame>
						<scale_factors>0.20000000000000001 0.20000000000000001 0.20000000000000001</scale_factors>
					</FrameGeometry>
					<attached_geometry>
{meshes}					</attached_geometry>
					<WrapObjectSet name="wrapobjectset">
						<objects />
						<groups />
					</WrapObjectSet>
					<mass>{mass}</mass>
					<mass_center>{cx} {cy} {cz}</mass_center>
					<inertia>{ixx} {iyy} {izz} {ixy} {ixz} {iyz}</inertia>
				</Body>
"""


def _coord_xml(name: str) -> str:
    r = CERVICAL_RANGE_RAD
    return f"""						<Coordinate name="{name}">
							<default_value>0</default_value>
							<default_speed_value>0</default_speed_value>
							<range>{-r} {r}</range>
							<clamped>true</clamped>
							<locked>false</locked>
							<prescribed_function />
							<prescribed>false</prescribed>
						</Coordinate>
"""


def _offset_frame(name: str, parent_body: str, translation: tuple[float, float, float]) -> str:
    tx, ty, tz = translation
    return f"""						<PhysicalOffsetFrame name="{name}">
							<FrameGeometry name="frame_geometry">
								<socket_frame>..</socket_frame>
								<scale_factors>0.20000000000000001 0.20000000000000001 0.20000000000000001</scale_factors>
							</FrameGeometry>
							<socket_parent>/bodyset/{parent_body}</socket_parent>
							<translation>{tx} {ty} {tz}</translation>
							<orientation>0 0 0</orientation>
						</PhysicalOffsetFrame>
"""


def _custom_joint(
    joint_name: str,
    parent_frame: str,
    child_frame: str,
    coords: tuple[str, str, str],
    parent_body: str,
    child_body: str,
    parent_trans: tuple[float, float, float],
    child_trans: tuple[float, float, float],
    parent_offset_name: str,
    child_offset_name: str,
) -> str:
    ext, bend, rot = coords
    return f"""				<CustomJoint name="{joint_name}">
					<socket_parent_frame>{parent_frame}</socket_parent_frame>
					<socket_child_frame>{child_frame}</socket_child_frame>
					<coordinates>
{_coord_xml(ext)}{_coord_xml(bend)}{_coord_xml(rot)}					</coordinates>
					<frames>
{_offset_frame(parent_offset_name, parent_body, parent_trans)}{_offset_frame(child_offset_name, child_body, child_trans)}					</frames>
					<SpatialTransform>
						<TransformAxis name="rotation1">
							<coordinates>{ext}</coordinates>
							<axis>0 0 1</axis>
							<LinearFunction name="function">
								<coefficients> 1 0</coefficients>
							</LinearFunction>
						</TransformAxis>
						<TransformAxis name="rotation2">
							<coordinates>{bend}</coordinates>
							<axis>1 0 0</axis>
							<LinearFunction name="function">
								<coefficients> 1 0</coefficients>
							</LinearFunction>
						</TransformAxis>
						<TransformAxis name="rotation3">
							<coordinates>{rot}</coordinates>
							<axis>0 1 0</axis>
							<LinearFunction name="function">
								<coefficients> 1 0</coefficients>
							</LinearFunction>
						</TransformAxis>
						<TransformAxis name="translation1">
							<coordinates></coordinates>
							<axis>1 0 0</axis>
							<Constant name="function">
								<value>0</value>
							</Constant>
						</TransformAxis>
						<TransformAxis name="translation2">
							<coordinates></coordinates>
							<axis>0 1 0</axis>
							<Constant name="function">
								<value>0</value>
							</Constant>
						</TransformAxis>
						<TransformAxis name="translation3">
							<coordinates></coordinates>
							<axis>0 0 1</axis>
							<Constant name="function">
								<value>0</value>
							</Constant>
						</TransformAxis>
					</SpatialTransform>
				</CustomJoint>
"""


def build_augmented_model(source: Path | None = None, dest: Path | None = None) -> Path:
    source = source or RAJAGOPAL_OSIM
    dest = dest or AUGMENTED_OSIM
    LOCAL_GEOMETRY_DIR.mkdir(parents=True, exist_ok=True)
    _shifted_official_mesh("hat_skull.vtp", "hat_skull_head.vtp", HEAD_IN_TORSO)
    _shifted_official_mesh("hat_jaw.vtp", "hat_jaw_head.vtp", HEAD_IN_TORSO)
    _neck_cylinder_vtp(LOCAL_GEOMETRY_DIR / "neck_visual.vtp")

    tree = ET.parse(source)
    root = tree.getroot()
    model = root.find("Model")
    assert model is not None
    model.set("name", "RajagopalLaiUhlrich2023_neck")

    bodies = root.find(".//BodySet/objects")
    assert bodies is not None
    torso = None
    for body in list(bodies):
        if body.attrib.get("name") == "torso":
            torso = body
            break
    assert torso is not None
    geom = torso.find("attached_geometry")
    assert geom is not None
    for mesh in list(geom.findall("Mesh")):
        mf = mesh.find("mesh_file")
        if mf is not None and mf.text in ("hat_jaw.vtp", "hat_skull.vtp"):
            geom.remove(mesh)
    mass = torso.find("mass")
    com = torso.find("mass_center")
    inertia = torso.find("inertia")
    if mass is not None:
        mass.text = str(TORSO_MASS_KG)
    if com is not None:
        com.text = f"{TORSO_COM[0]} {TORSO_COM[1]} {TORSO_COM[2]}"
    if inertia is not None:
        inertia.text = " ".join(str(v) for v in TORSO_INERTIA)

    neck_meshes = _mesh_xml("neck_geom_1", "neck_visual.vtp")
    head_meshes = _mesh_xml("head_geom_skull", "hat_skull_head.vtp") + _mesh_xml(
        "head_geom_jaw", "hat_jaw_head.vtp"
    )
    neck_el = ET.fromstring(_body_xml("neck", NECK_MASS_KG, NECK_COM, NECK_INERTIA, neck_meshes))
    head_el = ET.fromstring(_body_xml("head", HEAD_MASS_KG, HEAD_COM, HEAD_INERTIA, head_meshes))
    torso_index = list(bodies).index(torso)
    bodies.insert(torso_index + 1, neck_el)
    bodies.insert(torso_index + 2, head_el)

    joints = root.find(".//JointSet/objects")
    assert joints is not None
    back = None
    for joint in list(joints):
        if joint.attrib.get("name") == "back":
            back = joint
            break
    assert back is not None
    neck_joint = ET.fromstring(
        _custom_joint(
            "cervical_lower",
            "torso_c7_offset",
            "neck_c7_offset",
            ("neck_extension", "neck_bending", "neck_rotation"),
            "torso",
            "neck",
            NECK_IN_TORSO,
            (0.0, 0.0, 0.0),
            "torso_c7_offset",
            "neck_c7_offset",
        )
    )
    head_joint = ET.fromstring(
        _custom_joint(
            "cervical_upper",
            "neck_c1_offset",
            "head_c1_offset",
            ("head_extension", "head_bending", "head_rotation"),
            "neck",
            "head",
            HEAD_IN_NECK,
            (0.0, 0.0, 0.0),
            "neck_c1_offset",
            "head_c1_offset",
        )
    )
    back_index = list(joints).index(back)
    joints.insert(back_index + 1, neck_joint)
    joints.insert(back_index + 2, head_joint)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tree.write(dest, encoding="unicode", xml_declaration=True)
    return dest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=RAJAGOPAL_OSIM)
    parser.add_argument("--dest", type=Path, default=AUGMENTED_OSIM)
    args = parser.parse_args()
    path = build_augmented_model(args.source, args.dest)
    print(path)


if __name__ == "__main__":
    main()
