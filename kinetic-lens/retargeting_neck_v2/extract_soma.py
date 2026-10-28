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


def extract_session(
    session_id: str,
    *,
    device: str = "cuda:0",
    out_dir: Path | None = None,
    hpe_results: Path | None = None,
    fps: float | None = None,
) -> Path:
    """Write soma_raw.npz, soma_aligned.npz and extract_meta.json to ``out_dir``.

    The product passes the session's ik/ folder so these files are deleted with the session
    (account deletion, retention). ``OUTPUTS_DIR/<session>`` is only the default for dev scripts.

    ``hpe_results`` should be passed by the IK pipeline when known — session_paths can miss
    the file when SESSIONS_DIR points at a different checkout than CEREBEL_DATA_DIR.
    """
    paths = session_paths(session_id)
    hpe = Path(hpe_results).resolve() if hpe_results is not None else paths["hpe"]
    if hpe is None or not Path(hpe).is_file():
        raise FileNotFoundError(f"No hpe_results.pt for session {session_id}")
    hpe = Path(hpe)
    out_dir = Path(out_dir) if out_dir is not None else OUTPUTS_DIR / session_id
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
    use_fps = float(fps) if fps is not None else float(paths["fps"])
    times = np.arange(joints_osim.shape[0], dtype=np.float64) / use_fps
    seq = SomaSequence(
        joints_opensim=joints_osim,
        rotations_opensim=rotations_osim,
        times=times,
        fps=use_fps,
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
                "fps": use_fps,
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
