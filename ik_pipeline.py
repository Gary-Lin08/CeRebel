#!/usr/bin/env python3
"""Run Kinetic Lens OpenSim inverse kinematics on GEM-X SMPL output.

Production path uses the neck-aware Rajagopal model (retargeting_neck_v2):
torso → neck → head, with SOMA cervical landmarks + hand retargeting.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

LOG = logging.getLogger("cerebel.ik")

KINETIC_LENS_ROOT = Path(
    os.environ.get("CEREBEL_KINETIC_LENS_ROOT", str(Path(__file__).resolve().parent / "kinetic-lens"))
)
IK_PYTHON = Path(os.environ.get("CEREBEL_IK_PYTHON", str(Path(__file__).resolve().parent / ".venv-ik" / "bin" / "python")))
GEMX_PYTHON = Path(os.environ.get("CEREBEL_GEMX_PYTHON", "/root/GEM-X/.venv/bin/python"))
GEMX_ROOT = Path(os.environ.get("CEREBEL_GEMX_ROOT", "/root/GEM-X"))
SOMA_ASSETS = os.environ.get("CEREBEL_SOMA_ASSETS", str(GEMX_ROOT / "inputs" / "soma_assets"))
IK_ENABLED = os.environ.get("CEREBEL_IK_ENABLED", "1").lower() not in ("0", "false", "no")
SUBJECT_HEIGHT_CM = float(os.environ.get("CEREBEL_SUBJECT_HEIGHT_CM", "185"))
SUBJECT_MASS_KG = float(os.environ.get("CEREBEL_SUBJECT_MASS_KG", "93"))


@dataclass
class IkResult:
    session_id: str
    ik_dir: Path
    pose3d: Path
    opensim_motion: Path
    motion_data: Path
    estimated_dynamics: Path | None
    ok: bool
    error: str | None = None


def _shared_glb() -> Path:
    return KINETIC_LENS_ROOT / "public" / "assets" / "model" / "rajagopal-bones.glb"


def _opensim_model() -> Path:
    """Neck-augmented Rajagopal used by the production IK path."""
    return (
        KINETIC_LENS_ROOT
        / "retargeting_neck_v2"
        / "models"
        / "RajagopalLaiUhlrich2023_neck.osim"
    )


def _geometry_dirs() -> list[Path]:
    candidates = [
        KINETIC_LENS_ROOT / "analysis" / "Geometry",
        KINETIC_LENS_ROOT / "analysis" / "geometry",
        KINETIC_LENS_ROOT / "retargeting_neck_v2" / "models" / "geometry",
    ]
    return [p for p in candidates if p.is_dir()]


def _link_hpe_into_session(ik_dir: Path, hpe_results: Path) -> Path:
    """Symlink GEM-X hpe_results.pt into session/ik/ so SOMA extract can find it locally."""
    hpe_results = hpe_results.resolve()
    if not hpe_results.is_file():
        raise FileNotFoundError(f"missing hpe_results.pt: {hpe_results}")
    link = ik_dir / "hpe_results.pt"
    if link.is_symlink() or link.is_file():
        try:
            if link.resolve() == hpe_results:
                return link
        except FileNotFoundError:
            pass
        link.unlink()
    try:
        link.symlink_to(hpe_results)
    except OSError:
        # Some filesystems disallow symlinks; fall back to a hard link / copy.
        try:
            os.link(hpe_results, link)
        except OSError:
            shutil.copy2(hpe_results, link)
    return link


def _extract_soma_aligned(
    session_id: str,
    ik_dir: Path,
    device: str | None = None,
    *,
    hpe_results: Path | None = None,
    fps: float | None = None,
) -> Path:
    """Decode SOMA77 poses for neck landmarks; cache under session/ik/."""
    aligned = ik_dir / "soma_aligned.npz"
    if aligned.is_file():
        return aligned
    sys.path.insert(0, str(KINETIC_LENS_ROOT))
    from retargeting_neck_v2.extract_soma import extract_session

    device = device or os.environ.get("CEREBEL_IK_DEVICE", "cuda:0")
    # Write straight into session/ik/ so nothing about the user is left outside the session
    # folder (account deletion and retention remove the session folder).
    extracted = extract_session(
        session_id,
        device=device,
        out_dir=ik_dir,
        hpe_results=hpe_results,
        fps=fps,
    )
    if extracted.resolve() != aligned.resolve():
        shutil.copy2(extracted, aligned)
    return aligned


def _make_video(jpeg_dir: Path, output: Path, fps: float) -> bool:
    """Stitch session JPEGs into an MP4 for Kinetic Lens playback."""
    frames = sorted(jpeg_dir.glob("*.jpg"))
    if not frames:
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    list_file = output.with_suffix(".txt")
    list_file.write_text("\n".join(f"file '{f}'" for f in frames) + "\n", encoding="utf-8")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-r", str(fps),
        "-i", str(list_file),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(output),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=300)
        return output.is_file()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        LOG.warning("video stitch failed: %s", exc)
        return False
    finally:
        list_file.unlink(missing_ok=True)


def _wait_for_jpegs(
    session_dir: Path,
    jpeg_dir: Path,
    *,
    min_frames: int = 8,
    timeout_s: float = 600.0,
    poll_s: float = 1.0,
) -> list[Path]:
    """Wait for background web-upload JPEG extract when overlapping GEM-X.

    iOS / already-extracted sessions return immediately once enough frames exist.
    """
    upload = session_dir / "upload"
    has_source_video = bool(
        upload.is_dir() and any(p.is_file() for p in upload.glob("source_video.*"))
    )

    def _list() -> list[Path]:
        if not jpeg_dir.is_dir():
            return []
        return sorted(jpeg_dir.glob("*.jpg"))

    frames = _list()
    if len(frames) >= min_frames and not has_source_video:
        return frames
    if len(frames) >= min_frames:
        # Extract may still be appending; confirm count is stable.
        time.sleep(min(poll_s, 0.5))
        frames2 = _list()
        if len(frames2) == len(frames):
            return frames2
        frames = frames2

    if not has_source_video:
        return frames

    deadline = time.time() + timeout_s
    last_n = len(frames)
    stable = 0
    while time.time() < deadline:
        frames = _list()
        n = len(frames)
        if n >= min_frames:
            if n == last_n:
                stable += 1
                if stable >= 2:
                    return frames
            else:
                stable = 0
                last_n = n
        else:
            stable = 0
            last_n = n
        time.sleep(poll_s)
    frames = _list()
    LOG.warning(
        "timed out waiting for jpegs in %s (found %s, need ≥%s)",
        jpeg_dir,
        len(frames),
        min_frames,
    )
    return frames


def _run_estimated_dynamics(
    *,
    marked_model: Path,
    mot_path: Path,
    opensim_motion_path: Path,
    motion_data_path: Path,
    analysis_dir: Path,
    output_path: Path,
) -> bool:
    """Run OpenSim inverse dynamics with estimated GRF after IK."""

    script = KINETIC_LENS_ROOT / "scripts" / "run_estimated_dynamics.py"
    if not script.is_file():
        LOG.warning("estimated dynamics script missing: %s", script)
        return False
    if not marked_model.is_file() or not mot_path.is_file():
        LOG.warning("missing model or motion for dynamics: %s %s", marked_model, mot_path)
        return False

    dynamics_work = analysis_dir / "dynamics-work"
    dynamics_work.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(IK_PYTHON),
        str(script),
        "--model",
        str(marked_model.resolve()),
        "--motion",
        str(mot_path.resolve()),
        "--motion-json",
        str(opensim_motion_path.resolve()),
        "--analysis-dir",
        str(dynamics_work.resolve()),
        "--output",
        str(output_path.resolve()),
    ]
    if motion_data_path.is_file():
        cmd.extend(["--events-json", str(motion_data_path.resolve())])
    try:
        subprocess.run(cmd, check=True, timeout=900)
        return output_path.is_file()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        LOG.warning("estimated dynamics failed: %s", exc)
        return False


def run_ik_pipeline(
    session_id: str,
    session_dir: Path,
    hpe_results: Path,
    fps: float,
) -> IkResult:
    """Convert SMPL → pose3d → neck-aware OpenSim IK; write assets under session_dir/ik/."""
    session_dir = session_dir.resolve()
    hpe_results = hpe_results.resolve()
    ik_dir = session_dir / "ik"
    ik_dir.mkdir(parents=True, exist_ok=True)
    pose3d_path = ik_dir / "pose3d.json"
    motion_data_path = ik_dir / "motion-data.json"
    opensim_motion_path = ik_dir / "opensim-motion.json"
    estimated_dynamics_path = ik_dir / "estimated-dynamics.json"
    analysis_dir = ik_dir / "opensim-work"
    model_assets_dir = ik_dir / "model"

    try:
        # Step 1: SMPL → pose3d (GEM-X python, needs torch)
        LOG.info("converting SMPL to pose3d session=%s", session_id)
        header_path = session_dir / "header.json"
        pose3d_cmd = [
            str(GEMX_PYTHON), str(Path(__file__).resolve().parent / "smpl_to_pose3d.py"),
            "--hpe-results", str(hpe_results),
            "--output", str(pose3d_path),
            "--motion-data", str(motion_data_path),
            "--fps", str(fps),
            "--gemx-root", str(GEMX_ROOT),
            "--soma-assets", SOMA_ASSETS,
            "--height-cm", str(SUBJECT_HEIGHT_CM),
            "--mass-kg", str(SUBJECT_MASS_KG),
        ]
        if header_path.is_file():
            pose3d_cmd.extend(["--session-header", str(header_path)])
        subprocess.run(pose3d_cmd, check=True, timeout=600)

        # Link GEM-X output into the session so SOMA extract does not depend on SESSIONS_DIR.
        _link_hpe_into_session(ik_dir, hpe_results)

        # Step 1b: SOMA77 joint/pose pack for cervical landmarks
        LOG.info("extracting SOMA for neck-aware IK session=%s", session_id)
        soma_aligned = _extract_soma_aligned(
            session_id, ik_dir, hpe_results=hpe_results, fps=fps
        )

        # Step 2: OpenSim IK on Rajagopal + neck/head (retargeting_neck_v2)
        geometry = _geometry_dirs()
        if not geometry:
            raise FileNotFoundError("OpenSim geometry directory not found under kinetic-lens/analysis/")
        if not _opensim_model().is_file():
            # Build the augmented model on first use.
            sys.path.insert(0, str(KINETIC_LENS_ROOT))
            from retargeting_neck_v2.build_augmented_model import build_augmented_model

            build_augmented_model()
        if not _opensim_model().is_file():
            raise FileNotFoundError(f"missing OpenSim model: {_opensim_model()}")

        LOG.info("running OpenSim IK (neck-aware retarget) session=%s", session_id)
        ik_script = Path(__file__).resolve().parent / "run_opensim_ik_cerebel.py"
        cmd = [
            str(IK_PYTHON), str(ik_script),
            "--model", str(_opensim_model().resolve()),
            "--pose3d", str(pose3d_path.resolve()),
            "--soma-aligned", str(soma_aligned.resolve()),
            "--analysis-dir", str(analysis_dir.resolve()),
            "--public-model-dir", str(model_assets_dir.resolve()),
        ]
        for g in geometry:
            cmd.extend(["--geometry", str(g.resolve())])
        subprocess.run(cmd, check=True, timeout=1800)

        produced_motion = model_assets_dir / "opensim-motion.json"
        if not produced_motion.is_file():
            raise FileNotFoundError(f"IK did not produce {produced_motion}")
        if produced_motion != opensim_motion_path:
            shutil.copy2(produced_motion, opensim_motion_path)

        marked_model = analysis_dir / "KineticLens_markers.osim"
        if not marked_model.is_file():
            marked_model = analysis_dir / "neck_markers.osim"
        mot_path = analysis_dir / "cerebel-ik.mot"
        if not mot_path.is_file():
            mot_path = analysis_dir / "neck-ik.mot"
        dynamics_ok = _run_estimated_dynamics(
            marked_model=marked_model,
            mot_path=mot_path,
            opensim_motion_path=opensim_motion_path,
            motion_data_path=motion_data_path,
            analysis_dir=analysis_dir,
            output_path=estimated_dynamics_path,
        )
        if dynamics_ok:
            LOG.info("estimated dynamics ready session=%s → %s", session_id, estimated_dynamics_path)
        else:
            LOG.warning("estimated dynamics unavailable session=%s", session_id)

        # Log SOMA ↔ OpenSim alignment quality.
        manifest_alignment: dict = {}
        try:
            from cerebel_ik_retarget import SOMA_JOINT, alignment_error_report

            pose3d = json.loads(pose3d_path.read_text(encoding="utf-8"))
            osim = json.loads(opensim_motion_path.read_text(encoding="utf-8"))
            soma_pts = np.zeros((len(pose3d["frames"]), 77, 3))
            mp_to_joint = {
                "l_hip": SOMA_JOINT["l_hip"],
                "l_knee": SOMA_JOINT["l_knee"],
                "l_ankle": SOMA_JOINT["l_ankle"],
                "r_hip": SOMA_JOINT["r_hip"],
                "r_knee": SOMA_JOINT["r_knee"],
                "r_ankle": SOMA_JOINT["r_ankle"],
                "l_shoulder": SOMA_JOINT["l_shoulder"],
                "l_elbow": SOMA_JOINT["l_elbow"],
                "r_shoulder": SOMA_JOINT["r_shoulder"],
                "r_elbow": SOMA_JOINT["r_elbow"],
            }
            mp_idx = {
                "l_hip": 23, "l_knee": 25, "l_ankle": 27,
                "r_hip": 24, "r_knee": 26, "r_ankle": 28,
                "l_shoulder": 11, "l_elbow": 13,
                "r_shoulder": 12, "r_elbow": 14,
            }
            for t, frame in enumerate(pose3d["frames"]):
                w = np.array(frame["world"])
                for key, soma_idx in mp_to_joint.items():
                    soma_pts[t, soma_idx] = w[mp_idx[key]]
            manifest_alignment = alignment_error_report(soma_pts, osim["frames"])
            LOG.info("IK alignment report session=%s: %s", session_id, json.dumps(manifest_alignment))
        except Exception:
            LOG.exception("alignment report failed")

        # Prefer the neck-aware GLB written by IK; fall back to the shared asset.
        # Never overwrite a fresh neck GLB with the legacy shared bones.
        produced_glb = model_assets_dir / "rajagopal-bones.glb"
        if not produced_glb.is_file():
            neck_glb = model_assets_dir / "rajagopal-bones-neck.glb"
            if neck_glb.is_file():
                shutil.copy2(neck_glb, produced_glb)
            else:
                shared_glb = _shared_glb()
                if shared_glb.is_file():
                    shutil.copy2(shared_glb, produced_glb)
        elif produced_glb.is_file():
            # Keep the shared public asset in sync with the production model.
            shared_glb = _shared_glb()
            try:
                shared_glb.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(produced_glb, shared_glb)
            except OSError:
                LOG.warning("could not refresh shared GLB at %s", shared_glb)

        # Step 3: session video from JPEGs (may still be extracting in background
        # for web uploads that overlapped GEM-X with ffmpeg).
        jpeg_dir = session_dir / "jpegs"
        frames = _wait_for_jpegs(session_dir, jpeg_dir)
        if frames:
            _make_video(jpeg_dir, ik_dir / "session.mp4", fps)
            first_jpeg = frames[0]
            shutil.copy2(first_jpeg, ik_dir / "poster.jpg")
        else:
            LOG.warning("no jpegs available for session video stitch session=%s", session_id)

        # Write ik.json manifest
        manifest = {
            "session_id": session_id,
            "pose3d": str(pose3d_path),
            "motion_data": str(motion_data_path),
            "opensim_motion": str(opensim_motion_path),
            "estimated_dynamics": str(estimated_dynamics_path)
            if estimated_dynamics_path.is_file()
            else "",
            "glb": str(model_assets_dir / "rajagopal-bones.glb"),
            "video": str(ik_dir / "session.mp4") if (ik_dir / "session.mp4").is_file() else "",
            "fps": fps,
            "subject_height_cm": SUBJECT_HEIGHT_CM,
            "subject_mass_kg": SUBJECT_MASS_KG,
            "alignment": manifest_alignment,
            "ik_model": "RajagopalLaiUhlrich2023_neck",
            "soma_aligned": str(soma_aligned) if soma_aligned.is_file() else "",
        }
        (session_dir / "ik.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        LOG.info("IK ready session=%s → %s", session_id, opensim_motion_path)
        return IkResult(
            session_id=session_id,
            ik_dir=ik_dir,
            pose3d=pose3d_path,
            opensim_motion=opensim_motion_path,
            motion_data=motion_data_path,
            estimated_dynamics=estimated_dynamics_path
            if estimated_dynamics_path.is_file()
            else None,
            ok=True,
        )
    except Exception as exc:
        LOG.exception("IK failed for session %s", session_id)
        return IkResult(
            session_id=session_id,
            ik_dir=ik_dir,
            pose3d=pose3d_path,
            opensim_motion=opensim_motion_path,
            motion_data=motion_data_path,
            estimated_dynamics=estimated_dynamics_path
            if estimated_dynamics_path.is_file()
            else None,
            ok=False,
            error=str(exc),
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--session-dir", type=Path, required=True)
    p.add_argument("--hpe-results", type=Path, required=True)
    p.add_argument("--fps", type=float, default=30.0)
    args = p.parse_args()
    result = run_ik_pipeline(args.session_dir.name, args.session_dir, args.hpe_results, args.fps)
    raise SystemExit(0 if result.ok else 1)
