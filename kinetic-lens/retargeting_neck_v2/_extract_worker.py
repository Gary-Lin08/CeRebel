#!/usr/bin/env python3
"""GEM-X worker: decode SomaLayer joints + 77 local axis-angles. Invoked by extract_soma.py."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch


def _get_body_params(pred: dict, key: str):
    if key in pred:
        return pred[key]
    suffix = key.replace("body_params_", "")
    for k in pred:
        if k.endswith(f"_params_{suffix}"):
            return pred[k]
    raise KeyError(key)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hpe-results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gemx-root", type=Path, required=True)
    parser.add_argument("--soma-assets", type=str, required=True)
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()

    sys.path.insert(0, str(args.gemx_root))
    from gem.utils.soma_utils.soma_layer import SomaLayer

    device = args.device
    if device.startswith("cuda") and not torch.cuda.is_available():
        device = "cpu"

    pred = torch.load(args.hpe_results, weights_only=False)
    # Move straight to the target device (GEM-X to_cuda fails on CPU-only torch builds).
    params = {
        k: v.to(device) if torch.is_tensor(v) else v
        for k, v in _get_body_params(pred, "body_params_global").items()
    }

    soma = SomaLayer(
        data_root=args.soma_assets,
        low_lod=True,
        device=device,
        identity_model_type="mhr",
        mode="warp",
    )
    go = params["global_orient"]
    bp = params["body_pose"]
    if go.ndim == 3:
        b, t, _ = go.shape
        bp = bp.reshape(b, t, 76, 3)
        poses = torch.cat([go[:, :, None], bp], dim=2).reshape(b * t, 77, 3)
        ident = params["identity_coeffs"]
        if ident.ndim == 3:
            ident = ident.reshape(b * t, ident.shape[-1])
        scale = params.get("scale_params")
        if scale is not None and scale.ndim == 3:
            scale = scale.reshape(b * t, scale.shape[-1])
        transl = params.get("transl")
        if transl is not None and transl.ndim == 3:
            transl = transl.reshape(b * t, 3)
        chunk_params = {
            "global_orient": poses[:, 0],
            "body_pose": poses[:, 1:],
            "identity_coeffs": ident,
            "scale_params": scale,
            "transl": transl,
        }
        joints = []
        with torch.no_grad():
            step = 32
            n = poses.shape[0]
            for i in range(0, n, step):
                sl = {k: v[i : i + step] if v is not None else None for k, v in chunk_params.items()}
                out = soma(**sl)
                joints.append(out["joints"].float().cpu())
        joints_np = torch.cat(joints, dim=0).numpy()
        poses_np = poses.float().cpu().numpy()
    else:
        with torch.no_grad():
            out = soma(**params)
        joints_np = out["joints"].float().cpu().numpy()
        if bp.ndim == 2 and bp.shape[-1] == 76 * 3:
            bp = bp.reshape(bp.shape[0], 76, 3)
        poses_np = torch.cat([go[:, None], bp], dim=1).float().cpu().numpy()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        args.output,
        joints_raw=joints_np.astype(np.float64),
        poses_aa=poses_np.astype(np.float64),
        hpe=np.array([str(args.hpe_results)]),
    )
    print(json.dumps({"frames": int(joints_np.shape[0]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
