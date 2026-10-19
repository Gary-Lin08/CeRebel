#!/usr/bin/env python3
"""OpenSim IK for CeRebel — neck-aware Rajagopal (torso → neck → head).

Replaces the legacy skull-welded Rajagopal IK. Requires a SOMA-aligned joint
sequence (``--soma-aligned``) produced by ``retargeting_neck_v2.extract_soma``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KINETIC_LENS = ROOT / "kinetic-lens"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(KINETIC_LENS))

from retargeting_neck_v2.build_augmented_model import build_augmented_model  # noqa: E402
from retargeting_neck_v2.run_ik import run_neck_ik  # noqa: E402
from retargeting_neck_v2.soma_frames import load_npz  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        type=Path,
        default=None,
        help="Optional .osim override (defaults to RajagopalLaiUhlrich2023_neck.osim).",
    )
    parser.add_argument("--pose3d", type=Path, required=True)
    parser.add_argument(
        "--soma-aligned",
        type=Path,
        required=True,
        help="soma_aligned.npz from retargeting_neck_v2.extract_soma",
    )
    parser.add_argument("--geometry", type=Path, action="append", default=None)
    parser.add_argument("--analysis-dir", type=Path, required=True)
    parser.add_argument("--public-model-dir", type=Path, required=True)
    args = parser.parse_args()

    model_path = args.model
    if model_path is None or not model_path.is_file():
        model_path = build_augmented_model()

    if not args.soma_aligned.is_file():
        raise FileNotFoundError(f"missing SOMA aligned pack: {args.soma_aligned}")
    if not args.pose3d.is_file():
        raise FileNotFoundError(f"missing pose3d: {args.pose3d}")

    pose3d = json.loads(args.pose3d.read_text(encoding="utf-8"))
    seq = load_npz(args.soma_aligned)
    diagnostics = run_neck_ik(
        pose3d=pose3d,
        seq=seq,
        work_dir=args.analysis_dir,
        public_dir=args.public_model_dir,
        model_path=model_path,
        production_names=True,
    )
    motion_path = args.public_model_dir / "opensim-motion.json"
    metadata = json.loads(motion_path.read_text(encoding="utf-8"))["metadata"]
    print(json.dumps({"metadata": metadata, "diagnostics": diagnostics}, indent=2))


if __name__ == "__main__":
    main()
