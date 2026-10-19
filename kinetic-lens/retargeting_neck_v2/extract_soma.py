"""Extract SOMA77 joints and local poses for a CeRebel session."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from .config import GEMX_PYTHON, GEMX_ROOT, OUTPUTS_DIR, SOMA_ASSETS
from .soma_frames import SomaSequence, align_joints_and_rotations, save_npz, session_paths


def extract_session(session_id: str, *, device: str = "cuda:0") -> Path:
    paths = session_paths(session_id)
    hpe = paths["hpe"]
    if hpe is None:
        raise FileNotFoundError(f"No hpe_results.pt for session {session_id}")
    out_dir = OUTPUTS_DIR / session_id
    raw_path = out_dir / "soma_raw.npz"
    aligned_path = out_dir / "soma_aligned.npz"
    worker = Path(__file__).with_name("_extract_worker.py")
    cmd = [
        str(GEMX_PYTHON),
        str(worker),
        "--hpe-results",
        str(hpe),
        "--output",
        str(raw_path),
        "--gemx-root",
        str(GEMX_ROOT),
        "--soma-assets",
        str(SOMA_ASSETS),
        "--device",
        device,
    ]
    subprocess.run(cmd, check=True)
    raw = np.load(raw_path)
    joints_raw = raw["joints_raw"]
    poses_aa = raw["poses_aa"]
    joints_osim, rotations_osim, yaw = align_joints_and_rotations(joints_raw, poses_aa)
    fps = float(paths["fps"])
    times = np.arange(joints_osim.shape[0], dtype=np.float64) / fps
    seq = SomaSequence(
        joints_opensim=joints_osim,
        rotations_opensim=rotations_osim,
        times=times,
        fps=fps,
        global_yaw_rad=yaw,
        source=str(hpe),
        session_id=session_id,
    )
    save_npz(aligned_path, seq)
    (out_dir / "extract_meta.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "frames": int(joints_osim.shape[0]),
                "fps": fps,
                "hpe": str(hpe),
            },
            indent=2,
        )
        + "\n"
    )
    return aligned_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sequence", required=True)
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()
    path = extract_session(args.sequence, device=args.device)
    print(path)


if __name__ == "__main__":
    main()
