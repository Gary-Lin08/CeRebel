"""OpenSim skeleton clips for golf phase preview (Kinetic Lens path).

Uses the production SOMA → pose3d → OpenSim IK artifacts already written under
``session/ik/`` — the same ``opensim-motion.json`` + ``rajagopal-bones.glb`` that
Kinetic Lens ``OpenSimViewport`` drives. No second SOMA→SMPL-X joint fit.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

LOG = logging.getLogger("motion_agent.mesh.opensim_clip")

MAX_CLIP_FRAMES = 90


def _motion_path(session_dir: Path) -> Path | None:
    for cand in (
        session_dir / "ik" / "opensim-motion.json",
        session_dir / "ik" / "model" / "opensim-motion.json",
    ):
        if cand.is_file():
            return cand
    return None


def _glb_relpath(session_dir: Path) -> str | None:
    for rel in ("model/rajagopal-bones.glb", "rajagopal-bones.glb"):
        if (session_dir / "ik" / rel).is_file():
            return rel
    return None


def opensim_skeleton_status(session_dir: Path) -> dict[str, Any]:
    session_dir = session_dir.expanduser().resolve()
    motion = _motion_path(session_dir)
    glb_rel = _glb_relpath(session_dir)
    return {
        "error": False,
        "view": "opensim",
        "session_id": session_dir.name,
        "motion_available": motion is not None,
        "glb_available": glb_rel is not None,
        "motion_path": str(motion) if motion else None,
        "glb_url": (
            f"/media/{session_dir.name}/{glb_rel}" if glb_rel else None
        ),
        "pipeline": (
            "GEM-X SOMA → pose3d (cerebel_ik_retarget) → OpenSim IK "
            "(run_opensim_ik_cerebel) → opensim-motion.json + rajagopal-bones.glb"
        ),
    }


def _slim_muscles(muscles: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Round musculotendon path points for the viewer payload."""
    out: list[dict[str, Any]] = []
    if not muscles:
        return out
    for muscle in muscles:
        name = muscle.get("name")
        points = muscle.get("points") or []
        if not name or len(points) < 2:
            continue
        out.append(
            {
                "name": str(name),
                "points": [
                    [round(float(x), 4) for x in (pt or [0, 0, 0])[:3]]
                    for pt in points
                ],
            }
        )
    return out


def get_opensim_skeleton_clip(
    session_dir: Path,
    t0: float,
    t1: float,
    *,
    max_frames: int = MAX_CLIP_FRAMES,
    include_muscles: bool = True,
) -> dict[str, Any]:
    """Return a time-sliced OpenSim skeleton (+ muscle paths) clip for analysis."""
    session_dir = session_dir.expanduser().resolve()
    motion_path = _motion_path(session_dir)
    glb_rel = _glb_relpath(session_dir)
    if motion_path is None or glb_rel is None:
        return {
            "error": True,
            "fallback": "raw_video",
            "view": "opensim",
            "message": (
                "OpenSim skeleton assets missing "
                "(need ik/opensim-motion.json and ik/model/rajagopal-bones.glb). "
                "These come from the Kinetic Lens SOMA→OpenSim IK pipeline."
            ),
        }

    t0 = float(t0)
    t1 = float(t1)
    if not np.isfinite(t0) or not np.isfinite(t1) or t1 <= t0:
        return {
            "error": True,
            "fallback": "raw_video",
            "view": "opensim",
            "message": "invalid time window",
        }

    try:
        payload = json.loads(motion_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "error": True,
            "fallback": "raw_video",
            "view": "opensim",
            "message": f"failed to read opensim-motion.json: {exc}",
        }

    frames_in = payload.get("frames") or []
    if not frames_in:
        return {
            "error": True,
            "fallback": "raw_video",
            "view": "opensim",
            "message": "opensim-motion.json has no frames",
        }

    times = np.asarray([float(f.get("time", 0.0)) for f in frames_in], dtype=np.float64)
    # Inclusive window with tiny epsilon so end-locked phases still get a frame.
    mask = (times >= t0 - 1e-4) & (times <= t1 + 1e-4)
    idxs = np.nonzero(mask)[0]
    if idxs.size == 0:
        # Nearest frame fallback.
        i = int(np.argmin(np.abs(times - 0.5 * (t0 + t1))))
        idxs = np.asarray([i], dtype=np.int64)

    step = 1
    if idxs.size > int(max_frames):
        step = int(np.ceil(idxs.size / float(max_frames)))
        idxs = idxs[::step]

    body_names = list(payload.get("bodyNames") or [])
    if not body_names and frames_in:
        body_names = sorted((frames_in[0].get("bodies") or {}).keys())

    out_frames: list[dict[str, Any]] = []
    for i in idxs:
        fr = frames_in[int(i)]
        bodies_src = fr.get("bodies") or {}
        bodies: dict[str, Any] = {}
        for name in body_names:
            b = bodies_src.get(name)
            if not b:
                continue
            bodies[name] = {
                "p": [round(float(x), 5) for x in (b.get("p") or [0, 0, 0])],
                "q": [round(float(x), 6) for x in (b.get("q") or [0, 0, 0, 1])],
            }
        item: dict[str, Any] = {
            "time": round(float(fr.get("time", 0.0)), 4),
            "bodies": bodies,
        }
        if include_muscles:
            muscles = _slim_muscles(fr.get("muscles"))
            if muscles:
                item["muscles"] = muscles
        out_frames.append(item)

    meta = payload.get("metadata") or {}
    fps = None
    if len(times) >= 2:
        dt = float(np.median(np.diff(times)))
        if dt > 1e-6:
            fps = 1.0 / dt
    if not fps:
        fps = float(meta.get("fps") or meta.get("nominalFps") or 30.0)

    muscle_count = 0
    if out_frames and out_frames[0].get("muscles"):
        muscle_count = len(out_frames[0]["muscles"])
    elif include_muscles:
        muscle_count = int(meta.get("muscleCount") or 0)

    return {
        "error": False,
        "view": "opensim",
        "session_id": session_dir.name,
        "start_time": t0,
        "end_time": t1,
        "fps": float(fps) / float(step),
        "source_fps": float(fps),
        "frame_count": len(out_frames),
        "body_names": body_names,
        "frames": out_frames,
        "glb_url": f"/media/{session_dir.name}/{glb_rel}",
        "visual_scale": float(meta.get("visualScale") or 1.0),
        "model_name": meta.get("modelName"),
        "muscle_count": muscle_count,
        "include_muscles": bool(include_muscles and muscle_count > 0),
        "pipeline": meta.get("pipeline")
        or "GEM-X SOMA → cerebel_ik_retarget → OpenSim IK (Kinetic Lens)",
        "note": (
            "Analysis skeleton uses the Kinetic Lens OpenSim retarget "
            "(opensim-motion.json + rajagopal-bones.glb) with Rajagopal "
            "musculotendon path polylines when present."
        ),
    }


# Back-compat aliases used by chat_server / older UI paths.
def get_smplx_mesh_clip(session_dir: Path, t0: float, t1: float, **kwargs: Any) -> dict[str, Any]:
    return get_opensim_skeleton_clip(session_dir, t0, t1, **kwargs)


def smplx_mesh_status(session_dir: Path) -> dict[str, Any]:
    return opensim_skeleton_status(session_dir)
