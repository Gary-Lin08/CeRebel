"""Load the server-side constant average SMPL-X body shape for display."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_SHAPE_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "average_smplx_shape.json"
)


@lru_cache(maxsize=4)
def _load_raw(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"average shape file must be a JSON object: {path}")
    betas = data.get("betas")
    if not isinstance(betas, list) or not betas:
        raise ValueError(f"average shape file missing betas: {path}")
    return data


def average_smplx_shape_path() -> Path:
    return DEFAULT_SHAPE_PATH


def load_average_smplx_shape(
    path: Path | None = None,
) -> dict[str, Any]:
    """Return the full average-shape record (betas + metadata)."""
    p = (path or DEFAULT_SHAPE_PATH).expanduser().resolve()
    data = dict(_load_raw(str(p)))
    data["path"] = str(p)
    data["mtime_ns"] = p.stat().st_mtime_ns
    return data


def load_average_smplx_betas(
    path: Path | None = None,
    *,
    num_betas: int | None = None,
) -> np.ndarray:
    """Return float32 betas for SMPL-X forward (padded/truncated to num_betas)."""
    data = load_average_smplx_shape(path)
    betas = np.asarray(data["betas"], dtype=np.float32).reshape(-1)
    n = int(num_betas if num_betas is not None else data.get("num_betas") or len(betas))
    if betas.shape[0] < n:
        out = np.zeros(n, dtype=np.float32)
        out[: betas.shape[0]] = betas
        return out
    return betas[:n].copy()
