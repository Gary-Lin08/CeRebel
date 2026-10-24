#!/usr/bin/env python3
"""Strip what CeRebel does not need from a GEM-X hpe_results.pt once a capture is processed.

GEM-X saves ``{"body_params_global", "body_params_incam", "K_fullimg", "net_outputs"}``.
Downstream code (smpl_to_pose3d, neck IK, SOMA display export, mesh viewer) only reads the
first three. This rewrites the file in place so that it keeps only those, and:

- drops ``net_outputs`` (the network's intermediate outputs, a full copy of the predictions
  plus raw model output);
- zeroes the MHR head-shape coefficients, ``identity_coeffs[..., 20:40]`` (MHR identity is
  20 body + 20 head + 5 hand components). Zero is the mean head, so a mesh rebuilt later
  shows an average head. GEM-X predicts no facial-expression coefficients.

Run after every derived result has been written (gemx_pipeline does this), or to backfill:

    python hpe_privacy.py /root/GEM-X/outputs/cerebel/<session>/hpe_results.pt
    python hpe_privacy.py --root /root/GEM-X/outputs/cerebel      # every session
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

KEEP_KEYS = ("body_params_global", "body_params_incam", "K_fullimg")
HEAD_IDENTITY = slice(20, 40)
MARKER_KEY = "cerebel_privacy"
MARKER = {"version": 1, "head_identity_zeroed": True, "net_outputs_dropped": True}


def sanitize_pred(pred: dict) -> dict:
    """Return the stripped prediction dict (tensors are copied, not modified in place)."""
    out: dict = {}
    for key in KEEP_KEYS:
        if key not in pred:
            continue
        value = pred[key]
        if isinstance(value, dict):
            value = dict(value)
            ident = value.get("identity_coeffs")
            if ident is not None and ident.shape[-1] >= HEAD_IDENTITY.stop:
                ident = ident.clone()
                ident[..., HEAD_IDENTITY] = 0
                value["identity_coeffs"] = ident
        out[key] = value
    out[MARKER_KEY] = dict(MARKER)
    return out


def sanitize_file(path: Path) -> bool:
    """Rewrite ``path`` in place. Returns False when it was already stripped."""
    import torch

    pred = torch.load(path, map_location="cpu", weights_only=False)
    if isinstance(pred, dict) and pred.get(MARKER_KEY, {}).get("version") == MARKER["version"]:
        return False
    tmp = path.with_name(path.name + ".tmp")
    torch.save(sanitize_pred(pred), tmp)
    os.replace(tmp, path)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strip head shape and unused outputs from hpe_results.pt")
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--root", type=Path, help="sanitize every hpe_results.pt under this folder")
    args = parser.parse_args(argv)
    paths = list(args.paths)
    if args.root:
        paths += sorted(args.root.rglob("hpe_results.pt"))
    failed = 0
    for path in paths:
        try:
            changed = sanitize_file(path)
        except Exception as exc:  # noqa: BLE001 - report and continue with the next file
            failed += 1
            print(f"failed {path}: {exc}", file=sys.stderr)
            continue
        print(f"{'sanitized' if changed else 'already clean'} {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
