#!/usr/bin/env python3
"""Convert GEM-X SMPL/SOMA hpe_results.pt into Kinetic Lens pose3d JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

from cerebel_ik_retarget import (
    build_mediapipe_landmarks,
    global_yaw_series,
    normalize_soma77_joints,
    soma_yup_to_opensim,
)

LANDMARK_NAMES = [
    "nose", "left_eye_inner", "left_eye", "left_eye_outer",
    "right_eye_inner", "right_eye", "right_eye_outer",
    "left_ear", "right_ear", "mouth_left", "mouth_right",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_pinky", "right_pinky",
    "left_index", "right_index", "left_thumb", "right_thumb",
    "left_hip", "right_hip", "left_knee", "right_knee",
    "left_ankle", "right_ankle", "left_heel", "right_heel",
    "left_foot_index", "right_foot_index",
]


def _get_body_params(pred: dict, key: str):
    if key in pred:
        return pred[key]
    suffix = key.replace("body_params_", "")
    for k in pred:
        if k.endswith(f"_params_{suffix}"):
            return pred[k]
    raise KeyError(f"Missing `{key}` in prediction output.")


def _image_size_from_K(K: np.ndarray, header: dict | None) -> tuple[int, int]:
    if header and header.get("width") and header.get("height"):
        return int(header["width"]), int(header["height"])
    # Principal point is typically near image centre.
    return max(int(round(K[0, 2] * 2)), 1), max(int(round(K[1, 2] * 2)), 1)


def convert_hpe_to_pose3d(
    hpe_results: Path,
    *,
    fps: float,
    gemx_root: Path,
    soma_assets: str,
    device: str = "cuda:0",
    subject_height_cm: float = 185.0,
    subject_mass_kg: float = 93.0,
    session_header: dict | None = None,
) -> dict:
    """Return pose3d dict suitable for OpenSim IK with direct SOMA retargeting."""
    if str(gemx_root) not in sys.path:
        sys.path.insert(0, str(gemx_root))

    from gem.utils.net_utils import to_cuda
    from gem.utils.soma_utils.soma_layer import SomaLayer

    pred = torch.load(hpe_results, weights_only=False)
    K_all = pred["K_fullimg"].float().cpu().numpy()

    soma = SomaLayer(
        data_root=soma_assets,
        low_lod=True,
        device=device,
        identity_model_type="mhr",
        mode="warp",
    )
    with torch.no_grad():
        out_g = soma(**to_cuda(_get_body_params(pred, "body_params_global")))
        out_i = soma(**to_cuda(_get_body_params(pred, "body_params_incam")))
        raw_joints = out_g["joints"].float().cpu().numpy()
        global_yaw = global_yaw_series(raw_joints)
        joints_g = normalize_soma77_joints(raw_joints)
        joints_i = out_i["joints"].float().cpu().numpy()
    image_size = _image_size_from_K(K_all[0], session_header)

    frames = []
    for t in range(joints_g.shape[0]):
        opensim_pts = soma_yup_to_opensim(joints_g[t])
        world, image = build_mediapipe_landmarks(
            opensim_pts,
            joints_i[t],
            K_all[t],
            image_size,
        )
        visibility = np.full(33, 0.9, dtype=np.float64)
        frames.append(
            {
                "time": round(t / fps, 6),
                "image": np.round(image, 7).tolist(),
                "world": np.round(world, 7).tolist(),
                "visibility": np.round(visibility, 5).tolist(),
            }
        )

    return {
        "metadata": {
            "source": str(hpe_results),
            "method": "GEM-X SOMA77 global joints (SOMA77 yaw normalize) → OpenSim coordinates",
            "coordinateSystem": "OpenSim ground frame (Y-up, X-forward, Z-right) in metres",
            "fps": fps,
            "frameCount": len(frames),
            "subjectHeightCm": subject_height_cm,
            "subjectMassKg": subject_mass_kg,
            "imageSize": list(image_size),
            "globalYawRad": [round(float(v), 8) for v in global_yaw],
            "warning": "Monocular 3D estimate from GEM-X SMPL; not calibrated optical motion capture",
        },
        "landmarkNames": LANDMARK_NAMES,
        "frames": frames,
    }


def write_motion_data_stub(pose3d: dict, output: Path) -> None:
    """Minimal motion-data.json for Kinetic Lens video sync."""
    meta = pose3d["metadata"]
    fps = float(meta["fps"])
    n = int(meta["frameCount"])
    payload = {
        "metadata": {
            "source": meta.get("source", "GEM-X SMPL"),
            "subjectHeightCm": meta.get("subjectHeightCm", 185),
            "subjectMassKg": meta.get("subjectMassKg", 93),
            "frameCount": n,
            "nominalFps": fps,
            "durationSeconds": round(n / fps, 4),
            "status": "Kinematic preview — GEM-X SMPL → OpenSim IK",
        },
        "events": [],
        "frames": [
            {
                "keypoints": [[0.5, 0.5, 0.0]] * 13,
                "angles": {
                    "leftKnee": 0.0,
                    "rightKnee": 0.0,
                    "leftHip": 0.0,
                    "rightHip": 0.0,
                    "trunkLean": 0.0,
                },
            }
            for _ in range(n)
        ],
    }
    output.write_text(json.dumps(payload, separators=(",", ":")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hpe-results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--motion-data", type=Path, default=None)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--gemx-root", type=Path, default=Path("/root/GEM-X"))
    parser.add_argument("--soma-assets", default="/root/GEM-X/inputs/soma_assets")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--height-cm", type=float, default=185.0)
    parser.add_argument("--mass-kg", type=float, default=93.0)
    parser.add_argument("--session-header", type=Path, default=None)
    args = parser.parse_args()

    header = None
    if args.session_header and args.session_header.is_file():
        header = json.loads(args.session_header.read_text(encoding="utf-8"))

    pose3d = convert_hpe_to_pose3d(
        args.hpe_results,
        fps=args.fps,
        gemx_root=args.gemx_root,
        soma_assets=args.soma_assets,
        device=args.device,
        subject_height_cm=args.height_cm,
        subject_mass_kg=args.mass_kg,
        session_header=header,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(pose3d, separators=(",", ":")))
    if args.motion_data:
        write_motion_data_stub(pose3d, args.motion_data)
    print(json.dumps(pose3d["metadata"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
