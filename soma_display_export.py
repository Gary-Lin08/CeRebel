#!/usr/bin/env python3
"""Export GEM-X SOMA mesh sequences for the fake-egocentric display path.

Writes a compact binary pack that Kinetic Lens (and other UIs) can stream
instead of the raw third-person source video::

    display/soma-mesh.bin   — faces (int32) + verts (float32, T×V×3)
    display/soma-mesh.json  — metadata sidecar
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import struct
import sys
from pathlib import Path

import numpy as np
import torch

LOG = logging.getLogger("cerebel.soma_display")

GEMX_ROOT = Path(os.environ.get("CEREBEL_GEMX_ROOT", "/root/GEM-X")).resolve()
SOMA_ASSETS = os.environ.get(
    "CEREBEL_SOMA_ASSETS", str(GEMX_ROOT / "inputs" / "soma_assets")
)
DEVICE = os.environ.get("CEREBEL_GEMX_DEVICE", "cuda:0")

# Binary layout: magic + header + faces_i32 + verts_f16
MAGIC = b"SOMA"  # exactly 4 bytes (matches HDR 4s field)
# header: magic 4s, version u32, n_faces u32, n_verts u32, n_frames u32, fps f32, space u32
# version 2 = float16 verts; space: 0 = global, 1 = incam
HDR_FMT = "<4sIIIIfI"
HDR_SIZE = struct.calcsize(HDR_FMT)
FORMAT_VERSION = 2


def _ensure_gemx() -> None:
    if str(GEMX_ROOT) not in sys.path:
        sys.path.insert(0, str(GEMX_ROOT))


def _get_body_params(pred: dict, key: str):
    if key in pred:
        return pred[key]
    if key.startswith("body_params_"):
        suffix = key.replace("body_params_", "")
        target_tail = f"_params_{suffix}"
        for k in pred:
            if k.endswith(target_tail):
                return pred[k]
    raise KeyError(f"Missing `{key}` in prediction output.")


def _as_numpy(x) -> np.ndarray:
    if isinstance(x, torch.Tensor):
        return x.detach().float().cpu().numpy()
    return np.asarray(x, dtype=np.float32)


def _normalize_global(verts: torch.Tensor, joints: torch.Tensor):
    from gem.utils.geo_transform import apply_T_on_points, compute_T_ayfz2ay

    verts = verts.clone()
    joints = joints.clone()
    y_min = verts[:, :, 1].min()
    verts[:, :, 1] -= y_min
    joints[:, :, 1] -= y_min
    t_ay2ayfz = compute_T_ayfz2ay(joints[[0]], inverse=True)
    verts = apply_T_on_points(verts, t_ay2ayfz)
    joints = apply_T_on_points(joints, t_ay2ayfz)
    return verts, joints


def _normalize_incam_for_display(verts: np.ndarray) -> np.ndarray:
    """Camera-frame SOMA → Y-up display coords, stably framed for orbiting.

    Aligns using a mid-clip reference frame (median vertex) so a single bad
    detection cannot shove the whole sequence high above the camera target.
    """
    # SOMA/OpenCV camera: X right, Y down, Z forward → flip Y for Y-up.
    out = np.empty_like(verts, dtype=np.float32)
    out[..., 0] = verts[..., 0]
    out[..., 1] = -verts[..., 1]
    out[..., 2] = verts[..., 2]

    ref_idx = int(out.shape[0] // 2)
    ref = out[ref_idx]
    # Median is robust to limb extremities / occasional spikes.
    center = np.median(ref, axis=0).astype(np.float32)
    out -= center.reshape(1, 1, 3)

    # Sit feet near the ground plane using a low Y percentile of the ref frame.
    y_floor = float(np.percentile(out[ref_idx, :, 1], 5.0))
    out[..., 1] -= y_floor

    # Prefer the body in front of a +Z camera (OpenCV Z-forward stays negative
    # for subjects in front of the lens). Flip so the subject faces the viewer.
    out[..., 2] *= -1.0
    return out


def _normalize_global_for_display(verts: np.ndarray) -> np.ndarray:
    """Y-up global mesh with root translation removed (OpenSim-like in-place).

    Keeps articulation / orientation, but pins the body over the origin so the
    subject does not walk out of a fixed camera — matching the Kinetic Lens
    OpenSim panel framing.
    """
    out = np.asarray(verts, dtype=np.float32).copy()
    # Per-frame horizontal centering (avoid fancy-index axis reordering).
    centers_x = np.median(out[:, :, 0], axis=1)
    centers_z = np.median(out[:, :, 2], axis=1)
    out[:, :, 0] -= centers_x[:, None]
    out[:, :, 2] -= centers_z[:, None]
    # Shared ground plane from a low Y percentile across the clip.
    y_floor = float(np.percentile(out[:, :, 1], 5.0))
    out[..., 1] -= y_floor
    return out


def compute_soma_display_meshes(
    hpe_results: Path,
    *,
    space: str = "global",
    device: str = DEVICE,
    soma_assets: str = SOMA_ASSETS,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (faces int32 [F,3], verts float32 [T,V,3]) for browser display."""
    _ensure_gemx()
    from gem.utils.net_utils import to_cuda
    from gem.utils.soma_utils.soma_layer import SomaLayer

    pred = torch.load(hpe_results, weights_only=False)
    soma = SomaLayer(
        data_root=soma_assets,
        low_lod=True,
        device=device,
        identity_model_type="mhr",
        mode="warp",
    )
    faces = _as_numpy(soma.faces).astype(np.int32)
    # Prefer global (no camera translation). "incam" kept for optional demos.
    key = "body_params_incam" if space == "incam" else "body_params_global"

    with torch.no_grad():
        out = soma(**to_cuda(_get_body_params(pred, key)))
        verts_t = out["vertices"]
        if space == "incam":
            verts = _normalize_incam_for_display(_as_numpy(verts_t))
        else:
            verts_t, _ = _normalize_global(verts_t, out["joints"])
            verts = _normalize_global_for_display(_as_numpy(verts_t))

    return faces, np.ascontiguousarray(verts, dtype=np.float32)


def export_soma_display_mesh(
    hpe_results: Path,
    out_dir: Path,
    *,
    fps: float = 30.0,
    space: str = "global",
    device: str = DEVICE,
    soma_assets: str = SOMA_ASSETS,
) -> Path:
    """Write soma-mesh.bin + soma-mesh.json under ``out_dir``. Returns the bin path."""
    faces, verts = compute_soma_display_meshes(
        hpe_results, space=space, device=device, soma_assets=soma_assets
    )
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    bin_path = out_dir / "soma-mesh.bin"
    meta_path = out_dir / "soma-mesh.json"

    n_frames, n_verts, _ = verts.shape
    n_faces = int(faces.shape[0])
    space_id = 1 if space == "incam" else 0
    verts_f16 = np.ascontiguousarray(verts, dtype=np.float16)
    header = struct.pack(
        HDR_FMT,
        MAGIC,
        FORMAT_VERSION,
        n_faces,
        n_verts,
        n_frames,
        float(fps),
        space_id,
    )
    with bin_path.open("wb") as fh:
        fh.write(header)
        fh.write(np.ascontiguousarray(faces, dtype=np.int32).tobytes())
        fh.write(verts_f16.tobytes())

    meta = {
        "format": "soma1",
        "version": FORMAT_VERSION,
        "dtype": "float16",
        "fps": float(fps),
        "frame_count": int(n_frames),
        "vertex_count": int(n_verts),
        "face_count": int(n_faces),
        "space": space,
        "hpe_results": str(hpe_results),
        "bin": bin_path.name,
        "header_bytes": HDR_SIZE,
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    LOG.info(
        "exported SOMA display mesh → %s (%d frames, %d verts, space=%s)",
        bin_path,
        n_frames,
        n_verts,
        space,
    )
    return bin_path


def display_mesh_paths(session_dir: Path) -> tuple[Path, Path]:
    d = Path(session_dir) / "display"
    return d / "soma-mesh.bin", d / "soma-mesh.json"


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("CEREBEL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hpe-results", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument(
        "--space",
        choices=("incam", "global"),
        default=os.environ.get("CEREBEL_SOMA_DISPLAY_SPACE", "global"),
    )
    p.add_argument("--device", default=DEVICE)
    p.add_argument("--soma-assets", default=SOMA_ASSETS)
    args = p.parse_args()
    export_soma_display_mesh(
        args.hpe_results,
        args.out_dir,
        fps=args.fps,
        space=args.space,
        device=args.device,
        soma_assets=args.soma_assets,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
