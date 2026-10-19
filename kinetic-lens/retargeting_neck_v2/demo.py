"""End-to-end demo: extract SOMA, calibrate, IK, evaluate vs production Rajagopal IK."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# Allow `python kinetic-lens/retargeting_neck_v2/demo.py`
_PKG_DIR = Path(__file__).resolve().parent
_KL_ROOT = _PKG_DIR.parent
if str(_KL_ROOT) not in sys.path:
    sys.path.insert(0, str(_KL_ROOT))

from retargeting_neck_v2.anatomical_landmarks import (  # noqa: E402
    evaluate_hierarchy_candidates,
    load_or_init_offsets,
)
from retargeting_neck_v2.build_augmented_model import build_augmented_model  # noqa: E402
from retargeting_neck_v2.calibration import calibrate  # noqa: E402
from retargeting_neck_v2.checks import (  # noqa: E402
    check_markers,
    check_model,
    check_motion,
    write_check_report,
)
from retargeting_neck_v2.config import (  # noqa: E402
    AUGMENTED_OSIM,
    DEFAULT_SEQUENCE,
    OUTPUTS_DIR,
    SESSIONS_DIR,
)
from retargeting_neck_v2.evaluate import (  # noqa: E402
    evaluate_pair,
    write_comparison_video,
    write_plots,
)
from retargeting_neck_v2.extract_soma import extract_session  # noqa: E402
from retargeting_neck_v2.generate_markers import combined_targets, dump_marker_config  # noqa: E402
from retargeting_neck_v2.run_ik import run_neck_ik  # noqa: E402
from retargeting_neck_v2.soma_frames import load_npz  # noqa: E402


def _copy_old(session_id: str, dest: Path) -> dict:
    session = SESSIONS_DIR / session_id
    dest.mkdir(parents=True, exist_ok=True)
    motion_src = session / "ik" / "opensim-motion.json"
    glb_src = session / "ik" / "model" / "rajagopal-bones.glb"
    if not motion_src.is_file():
        raise FileNotFoundError(f"old pipeline motion missing: {motion_src}")
    shutil.copy2(motion_src, dest / "opensim-motion.json")
    if glb_src.is_file():
        shutil.copy2(glb_src, dest / "rajagopal-bones.glb")
    return json.loads(motion_src.read_text())


def run_demo(session_id: str, *, device: str = "cuda:0", skip_extract: bool = False) -> dict:
    out = OUTPUTS_DIR / session_id
    old_dir = out / "old"
    new_dir = out / "new"
    plots = out / "plots"
    work = out / "ik-work"
    out.mkdir(parents=True, exist_ok=True)

    model_path = build_augmented_model()
    check_model(model_path)
    load_or_init_offsets()

    aligned = out / "soma_aligned.npz"
    if not skip_extract or not aligned.is_file():
        extract_session(session_id, device=device)
    seq = load_npz(aligned)
    calib = calibrate(seq)
    hierarchy = evaluate_hierarchy_candidates(seq)
    (out / "hierarchy_report.json").write_text(json.dumps(hierarchy, indent=2) + "\n")

    pose3d = json.loads((SESSIONS_DIR / session_id / "ik" / "pose3d.json").read_text())
    times, targets = combined_targets(pose3d, seq)
    check_markers(targets)
    dump_marker_config(out / "marker_config.json")

    old_motion = _copy_old(session_id, old_dir)
    diag = run_neck_ik(
        pose3d=pose3d,
        seq=seq,
        work_dir=work,
        public_dir=new_dir,
        model_path=model_path,
    )
    new_motion = json.loads((new_dir / "opensim-motion.json").read_text())
    check_motion(new_motion, times)
    write_check_report(out / "sanity_checks.json", ["model_ok", "markers_ok", "ik_ok", "continuity_ok"])

    metrics = evaluate_pair(seq, old_motion, new_motion)
    slim = {k: v for k, v in metrics.items() if k != "series"}
    slim["hierarchy"] = hierarchy
    slim["calibration_neutral_index"] = calib["neutral_index"]
    slim["ik"] = diag
    (out / "metrics.json").write_text(json.dumps(slim, indent=2) + "\n")
    write_plots(metrics, plots)
    video = write_comparison_video(
        old_motion,
        new_motion,
        plots / "old_vs_new.mp4",
        seq.fps,
    )
    report = {
        "sequence": session_id,
        "model": str(model_path),
        "old_head_rmse_deg": slim["head_orientation_error_deg"]["old_RMSE_deg"],
        "new_head_rmse_deg": slim["head_orientation_error_deg"]["new_RMSE_deg"],
        "improvement_percent": slim["head_orientation_error_deg"]["improvement_percent"],
        "video": str(video),
        "outputs": str(out),
    }
    (out / "REPORT.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Neck-aware Rajagopal retargeting demo")
    parser.add_argument("--sequence", default=DEFAULT_SEQUENCE)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--skip-extract", action="store_true")
    args = parser.parse_args()
    report = run_demo(args.sequence, device=args.device, skip_extract=args.skip_extract)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
