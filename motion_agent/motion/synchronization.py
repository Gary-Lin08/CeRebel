"""Canonical timestamp synchronization across video, motion, and OpenSim."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class SyncMeta:
    fps: float
    frame_count: int
    duration_s: float
    coordinate_system: str = "OpenSim ground (Y-up, X-forward, Z-right)"
    time_origin: str = "seconds from video start"


def clamp_timestamp(t: float, duration_s: float) -> float:
    if duration_s <= 0:
        return 0.0
    return float(np.clip(t, 0.0, duration_s))


def timestamp_to_frame(t: float, fps: float, frame_count: int) -> int:
    if frame_count <= 0:
        raise ValueError("frame_count must be positive")
    if fps <= 0:
        raise ValueError("fps must be positive")
    idx = int(round(t * fps))
    return int(np.clip(idx, 0, frame_count - 1))


def frame_to_timestamp(frame_index: int, fps: float) -> float:
    return float(frame_index) / float(fps)


def nearest_index(timestamps: Sequence[float] | np.ndarray, t: float) -> int:
    arr = np.asarray(timestamps, dtype=np.float64)
    if arr.size == 0:
        raise ValueError("empty timestamps")
    return int(np.argmin(np.abs(arr - t)))


def interpolate_series(
    timestamps: np.ndarray,
    values: np.ndarray,
    t: float,
) -> np.ndarray:
    """Linear interpolate along axis 0. values shape (T, ...)."""
    ts = np.asarray(timestamps, dtype=np.float64)
    vals = np.asarray(values, dtype=np.float64)
    if ts.size == 0:
        raise ValueError("empty timestamps")
    if t <= ts[0]:
        return vals[0].copy()
    if t >= ts[-1]:
        return vals[-1].copy()
    i1 = int(np.searchsorted(ts, t, side="right"))
    i0 = i1 - 1
    w = (t - ts[i0]) / max(ts[i1] - ts[i0], 1e-12)
    return (1.0 - w) * vals[i0] + w * vals[i1]


def slice_indices(
    timestamps: np.ndarray,
    start_time: float,
    end_time: float,
) -> slice:
    ts = np.asarray(timestamps, dtype=np.float64)
    if end_time < start_time:
        start_time, end_time = end_time, start_time
    i0 = int(np.searchsorted(ts, start_time, side="left"))
    i1 = int(np.searchsorted(ts, end_time, side="right"))
    i0 = max(0, min(i0, len(ts)))
    i1 = max(i0, min(i1, len(ts)))
    return slice(i0, i1)
