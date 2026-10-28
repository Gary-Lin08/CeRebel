"""OpenSim IK for the augmented neck model (production CeRebel IK path)."""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import numpy as np

from .config import (
    AUGMENTED_OSIM,
    GEOMETRY_DIR,
    LOCAL_GEOMETRY_DIR,
    MARKER_WEIGHTS,
    PACKAGE_DIR,
    SUBJECT_MASS_KG,
    VISUAL_SCALE,
)
from .generate_markers import attach_markers, combined_targets, write_trc
from .soma_frames import SomaSequence


def _scripts_path() -> None:
    scripts = str(PACKAGE_DIR.parent / "scripts")
    repo = str(PACKAGE_DIR.parents[1])
    for p in (scripts, repo):
        if p not in sys.path:
            sys.path.insert(0, p)


def scale_model(model_path: Path, scaled_path: Path, geometry_dirs: list[Path]):
    import opensim as osim
    from run_opensim_ik import relax_pelvis_boundaries

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
    model.setName("KineticLens_RajagopalLaiUhlrich2023_neck_185cm_93kg")
    model.printToXML(str(scaled_path))
    return osim.Model(str(scaled_path))


def run_ik(model_path: Path, marker_path: Path, motion_path: Path, times: list[float]) -> None:
    import opensim as osim

    tool = osim.InverseKinematicsTool()
    tool.setName("KineticLens_NeckV2_IK")
    model = osim.Model(str(model_path))
    tool.setModel(model)
    tool.setMarkerDataFileName(str(marker_path))
    tool.setStartTime(times[0])
    tool.setEndTime(times[-1])
    tool.setOutputMotionFileName(str(motion_path))
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
        raise RuntimeError("OpenSim inverse kinematics failed")


def _postprocess_motion(
    frames: list[dict],
    yaw: np.ndarray,
    *,
    pose3d: dict | None = None,
) -> None:
    _scripts_path()
    from cerebel_ik_retarget import (
        apply_global_yaw_to_motion,
        hold_pose_after_root_jumps,
        make_body_quaternions_continuous,
        unwrap_coordinates,
    )

    body_names = list(frames[0]["bodies"])
    apply_global_yaw_to_motion(frames, yaw[: len(frames)])
    make_body_quaternions_continuous(frames, body_names)
    # Full-pose hold after ground_pelvis ZXY basin flips (pelvis-only retarget desyncs hips).
    hold_pose_after_root_jumps(frames)
    make_body_quaternions_continuous(frames, body_names)
    unwrap_coordinates(
        frames,
        (
            "pelvis_tilt",
            "pelvis_list",
            "pelvis_rotation",
            "neck_extension",
            "neck_bending",
            "neck_rotation",
            "head_extension",
            "head_bending",
            "head_rotation",
            "lumbar_extension",
            "lumbar_bending",
            "lumbar_rotation",
        ),
    )


def run_neck_ik(
    *,
    pose3d: dict,
    seq: SomaSequence,
    work_dir: Path,
    public_dir: Path,
    model_path: Path | None = None,
    production_names: bool = True,
) -> dict:
    """Solve neck-aware IK and write OpenSim motion + bone GLB.

    When ``production_names`` is true (default), also emit the filenames the
    CeRebel pipeline expects: ``KineticLens_markers.osim``, ``cerebel-ik.mot``,
    and ``rajagopal-bones.glb``.
    """
    _scripts_path()
    from export_opensim_assets import export_glb
    from run_opensim_ik import export_solved_motion
    from cerebel_ik_retarget import apply_hand_retargeting_to_motion

    import opensim as osim
    import shutil

    work_dir.mkdir(parents=True, exist_ok=True)
    public_dir.mkdir(parents=True, exist_ok=True)
    geometry = [GEOMETRY_DIR, LOCAL_GEOMETRY_DIR]
    for d in geometry:
        osim.ModelVisualizer.addDirToGeometrySearchPaths(str(d))

    model_path = model_path or AUGMENTED_OSIM
    if not Path(model_path).is_file():
        from .build_augmented_model import build_augmented_model

        model_path = build_augmented_model()

    scaled_path = work_dir / "neck_scaled.osim"
    marked_path = work_dir / "neck_markers.osim"
    trc_path = work_dir / "neck-markers.trc"
    mot_path = work_dir / "neck-ik.mot"

    model = scale_model(model_path, scaled_path, geometry)
    attach_markers(model, marked_path)
    times, targets = combined_targets(pose3d, seq)
    fps = float(pose3d["metadata"]["fps"])
    write_trc(trc_path, times, targets, fps)
    run_ik(marked_path, trc_path, mot_path, times)

    glb_neck = public_dir / "rajagopal-bones-neck.glb"
    glb_prod = public_dir / "rajagopal-bones.glb"
    export_glb(marked_path, geometry, glb_neck)
    if production_names:
        shutil.copy2(glb_neck, glb_prod)
        shutil.copy2(marked_path, work_dir / "KineticLens_markers.osim")
        shutil.copy2(mot_path, work_dir / "cerebel-ik.mot")
        shutil.copy2(scaled_path, work_dir / "KineticLens_scaled.osim")

    motion_json = public_dir / "opensim-motion.json"
    export_solved_motion(marked_path, mot_path, motion_json, targets)
    motion = json.loads(motion_json.read_text())
    yaw = seq.global_yaw_rad
    pose_yaw = pose3d.get("metadata", {}).get("globalYawRad")
    if pose_yaw:
        yaw = np.asarray(pose_yaw, dtype=np.float64)
    # Palm SOMA retarget twists hand_* off the locked radius_hand wrist and makes
    # the carpal pile look broken. Default is the classic locked-wrist IK (hand
    # follows radius). Opt in with CEREBEL_HAND_RETARGET=1.
    if os.environ.get("CEREBEL_HAND_RETARGET", "0").lower() in ("1", "true", "yes", "on"):
        apply_hand_retargeting_to_motion(motion["frames"], pose3d)
    _postprocess_motion(motion["frames"], yaw, pose3d=pose3d)
    motion["metadata"]["pipeline"] = (
        "SOMA anatomical landmarks → augmented Rajagopal (torso→neck→head) → OpenSim IK"
    )
    motion["metadata"]["modelVersion"] = "OpenSim 4.6 / Rajagopal-Lai-Uhlrich 2023 + cervical v2"
    motion["metadata"]["modelName"] = "KineticLens_RajagopalLaiUhlrich2023_neck_185cm_93kg"
    motion["metadata"]["warning"] = (
        "Kinematic neck model. Inertial properties of neck/head are estimates; "
        "not valid physiological inverse dynamics."
    )
    # Optional: MoBL-ARMS upper-limb musculotendon paths (right model + mirrored left).
    mobl_meta = None
    try:
        if os.environ.get("CEREBEL_MOBL_ARMS", "1").lower() not in ("0", "false", "no", "off"):
            _scripts_path()
            from mobl_arms_enrich import enrich_opensim_motion

            enrich_opensim_motion(motion, work_dir / "mobl-work", enabled=True)
            mobl_meta = (motion.get("metadata") or {}).get("moblArms")
    except Exception as exc:
        # Soft-fail — Rajagopal lower-limb muscles remain.
        mobl_meta = {"enabled": False, "error": str(exc)}

    motion_json.write_text(json.dumps(motion, separators=(",", ":")))
    diagnostics = {
        "n_frames": len(motion["frames"]),
        "start": times[0],
        "end": times[-1],
        "mean_marker_rmse_m": motion["metadata"].get("meanMarkerRmseM"),
        "bodies": motion["bodyNames"],
        "has_neck": "neck" in motion["bodyNames"],
        "has_head": "head" in motion["bodyNames"],
        "nan_coords": False,
        "marked_model": str(work_dir / "KineticLens_markers.osim")
        if production_names
        else str(marked_path),
        "mot": str(work_dir / "cerebel-ik.mot") if production_names else str(mot_path),
        "glb": str(glb_prod if production_names else glb_neck),
        "mobl_arms": mobl_meta,
        "muscle_count": (motion.get("metadata") or {}).get("muscleCount"),
    }
    for frame in motion["frames"]:
        for value in frame["coordinates"].values():
            if isinstance(value, float) and math.isnan(value):
                diagnostics["nan_coords"] = True
    (work_dir / "ik_diagnostics.json").write_text(json.dumps(diagnostics, indent=2) + "\n")
    return diagnostics


def main() -> None:
    import argparse
    from .extract_soma import extract_session
    from .soma_frames import load_npz
    from .config import DEFAULT_SEQUENCE, OUTPUTS_DIR, SESSIONS_DIR
    from .build_augmented_model import build_augmented_model

    parser = argparse.ArgumentParser()
    parser.add_argument("--sequence", default=DEFAULT_SEQUENCE)
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()
    build_augmented_model()
    aligned = OUTPUTS_DIR / args.sequence / "soma_aligned.npz"
    if not aligned.is_file():
        extract_session(args.sequence, device=args.device)
    seq = load_npz(aligned)
    pose3d = json.loads((SESSIONS_DIR / args.sequence / "ik" / "pose3d.json").read_text())
    out = OUTPUTS_DIR / args.sequence
    print(json.dumps(run_neck_ik(pose3d=pose3d, seq=seq, work_dir=out / "ik-work", public_dir=out / "new"), indent=2))


if __name__ == "__main__":
    main()
