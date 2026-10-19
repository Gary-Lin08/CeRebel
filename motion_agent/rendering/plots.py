"""Encode matplotlib figures as PNG for VLM / UI consumption."""

from __future__ import annotations

import base64
import io
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np


def fig_to_png_base64(fig: Any, dpi: int = 110) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def plot_time_series(
    times: np.ndarray,
    series: dict[str, np.ndarray],
    *,
    title: str,
    ylabel: str,
    xlabel: str = "time (s)",
) -> str:
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    fig.patch.set_facecolor("#f4f1ea")
    ax.set_facecolor("#fbf8f1")
    for name, vals in series.items():
        ax.plot(times, vals, label=name, linewidth=1.6)
    ax.set_title(title, fontsize=11)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if len(series) > 1:
        ax.legend(fontsize=8, frameon=False)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig_to_png_base64(fig)


def plot_com_height(
    times: np.ndarray,
    height_m: np.ndarray,
    vy: np.ndarray | None = None,
) -> str:
    fig, axes = plt.subplots(2 if vy is not None else 1, 1, figsize=(7.2, 4.4 if vy is not None else 3.2), sharex=True)
    if vy is None:
        axes = [axes]
    fig.patch.set_facecolor("#f4f1ea")
    axes[0].set_facecolor("#fbf8f1")
    axes[0].plot(times, height_m, color="#1f4e5f", linewidth=1.7)
    axes[0].set_ylabel("COM height (m)")
    axes[0].grid(True, alpha=0.3)
    if vy is not None:
        axes[1].set_facecolor("#fbf8f1")
        axes[1].plot(times, vy, color="#b85c38", linewidth=1.7)
        axes[1].axhline(0.0, color="#888", linewidth=0.8)
        axes[1].set_ylabel("COM v_y (m/s)")
        axes[1].grid(True, alpha=0.3)
    axes[-1].set_xlabel("time (s)")
    fig.suptitle("Center of mass (OpenSim Y-up)", fontsize=11)
    fig.tight_layout()
    return fig_to_png_base64(fig)
