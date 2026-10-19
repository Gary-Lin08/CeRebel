"""Sagittal OpenSim skeleton + dynamics overlay for Qwen3-VL."""

from __future__ import annotations

from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Circle, FancyArrowPatch
import numpy as np

from motion_agent.motion.sequence import MotionSequence
from motion_agent.motion.synchronization import clamp_timestamp, nearest_index, timestamp_to_frame
from motion_agent.rendering.plots import fig_to_png_base64
from motion_agent.storage.cache import GLOBAL_CACHE
from motion_agent.storage.errors import tool_error

# OpenSim body origins already mapped onto SMPL-X names in MotionSequence.
SKELETON_BONES: tuple[tuple[str, str], ...] = (
    ("pelvis", "left_hip"),
    ("left_hip", "left_knee"),
    ("left_knee", "left_ankle"),
    ("left_ankle", "left_foot"),
    ("pelvis", "right_hip"),
    ("right_hip", "right_knee"),
    ("right_knee", "right_ankle"),
    ("right_ankle", "right_foot"),
    ("pelvis", "spine3"),
    ("spine3", "left_shoulder"),
    ("left_shoulder", "left_elbow"),
    ("left_elbow", "left_wrist"),
    ("spine3", "right_shoulder"),
    ("right_shoulder", "right_elbow"),
    ("right_elbow", "right_wrist"),
)

MOMENT_JOINTS = ("left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle")
# Sagittal view uses OpenSim X (forward) vs Y (up).
GRF_ARROW_SCALE = 0.00028  # metres per Newton
MOMENT_RADIUS_SCALE = 0.0016  # metres per N·m


def _xy(seq: MotionSequence, name: str, idx: int) -> np.ndarray | None:
    pos = seq.joint_positions.get(name)
    if pos is None:
        return None
    return np.array([pos[idx, 0], pos[idx, 1]], dtype=np.float64)


def _first_xy(seq: MotionSequence, idx: int, *names: str) -> np.ndarray | None:
    for name in names:
        p = _xy(seq, name, idx)
        if p is not None:
            return p
    return None


def render_force_visualization(
    seq: MotionSequence,
    timestamp: float,
    include_com_trail: bool = True,
) -> dict[str, Any]:
    """Render reconstructed skeleton + estimated GRF/moments at a timestamp.

    This is a sagittal OpenSim-body stick figure, not a full SMPL-X mesh.
    Use it to spatially ground dynamics — not as a source of numerical values.
    """
    try:
        t = float(timestamp)
    except (TypeError, ValueError):
        return tool_error("INVALID_ARGUMENT", "timestamp must be a float")
    if t < -1e-6 or t > seq.duration_s + 0.05:
        return tool_error(
            "TIMESTAMP_OUT_OF_RANGE",
            f"timestamp {t} outside [0, {seq.duration_s:.4f}]",
        )
    t = clamp_timestamp(t, seq.duration_s)
    idx = nearest_index(seq.timestamps, t)
    actual_t = float(seq.timestamps[idx])
    cache_key = GLOBAL_CACHE.make_key(
        "force_vis",
        {"video_id": seq.video_id, "idx": idx, "trail": include_com_trail},
    )
    cached = GLOBAL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    fig, ax = plt.subplots(figsize=(7.4, 6.2))
    fig.patch.set_facecolor("#eef3f6")
    ax.set_facecolor("#eef3f6")

    xs, ys = [], []
    for a, b in SKELETON_BONES:
        pa, pb = _xy(seq, a, idx), _xy(seq, b, idx)
        if pa is None or pb is None:
            continue
        ax.plot([pa[0], pb[0]], [pa[1], pb[1]], color="#1d2b36", linewidth=2.4, zorder=3)
        xs.extend([pa[0], pb[0]])
        ys.extend([pa[1], pb[1]])
        ax.scatter([pa[0], pb[0]], [pa[1], pb[1]], s=18, color="#1d2b36", zorder=4)

    # Contact markers
    for side, joint, color in (
        ("left", "left_foot", "#2a7f70"),
        ("right", "right_foot", "#b85c38"),
    ):
        p = _first_xy(seq, idx, joint, joint.replace("foot", "ankle"))
        if p is None:
            continue
        in_contact = bool(seq.contact_left[idx] if side == "left" else seq.contact_right[idx])
        ax.scatter(
            [p[0]],
            [p[1]],
            s=70 if in_contact else 28,
            c=color,
            marker="s" if in_contact else "o",
            zorder=6,
            label=f"{side} contact={'yes' if in_contact else 'no'}",
        )

    # GRF arrows from feet (ground Y=0 if available)
    if seq.has_dynamics():
        for side, joint, force in (
            ("left", "left_foot", seq.grf_left_n[idx]),
            ("right", "right_foot", seq.grf_right_n[idx]),
        ):
            origin = _first_xy(seq, idx, joint, joint.replace("foot", "ankle"))
            if origin is None:
                continue
            mag = float(np.linalg.norm(force))
            if mag < 20:
                continue
            dx, dy = force[0] * GRF_ARROW_SCALE, force[1] * GRF_ARROW_SCALE
            ax.add_patch(
                FancyArrowPatch(
                    (origin[0], 0.0 if origin[1] > 0 else origin[1]),
                    (origin[0] + dx, (0.0 if origin[1] > 0 else origin[1]) + dy),
                    arrowstyle="-|>",
                    mutation_scale=14,
                    linewidth=1.8,
                    color="#c0392b",
                    zorder=7,
                )
            )
            ax.text(
                origin[0] + dx,
                (0.0 if origin[1] > 0 else origin[1]) + dy + 0.03,
                f"{side[0].upper()} GRF {mag:.0f} N",
                fontsize=7,
                color="#8b1e14",
            )

        # Flexion-moment magnitude as disks at joints
        for joint in MOMENT_JOINTS:
            p = _xy(seq, joint, idx)
            mom = seq.joint_flexion_moment_nm.get(joint)
            if p is None or mom is None:
                continue
            m = float(mom[idx])
            r = min(0.12, max(0.02, abs(m) * MOMENT_RADIUS_SCALE))
            color = "#2e86ab" if m >= 0 else "#e07a6a"
            ax.add_patch(Circle((p[0], p[1]), r, fill=False, linewidth=1.6, edgecolor=color, zorder=5))

    # COM
    com = seq.com_position[idx]
    ax.scatter([com[0]], [com[1]], s=90, marker="D", c="#f4c430", edgecolors="#333", zorder=8, label="COM")
    if include_com_trail:
        i0 = max(0, idx - int(seq.fps * 0.8))
        trail = seq.com_position[i0 : idx + 1]
        ax.plot(trail[:, 0], trail[:, 1], color="#f4c430", linewidth=1.2, alpha=0.7, zorder=2)

    ax.axhline(0.0, color="#9aa7b2", linewidth=0.8, linestyle="--")
    ax.set_aspect("equal", adjustable="box")
    if xs:
        pad = 0.35
        ax.set_xlim(min(xs) - pad, max(xs) + pad)
        ax.set_ylim(min(-0.05, min(ys) - pad), max(ys) + pad)
    ax.set_xlabel("X forward (m)")
    ax.set_ylabel("Y up (m)")
    ax.set_title(f"OpenSim skeleton + estimated dynamics @ {actual_t:.3f} s")
    ax.legend(loc="upper right", fontsize=7, frameon=False)
    ax.grid(True, alpha=0.2)
    fig.tight_layout()

    b64 = fig_to_png_base64(fig)
    result = {
        "error": False,
        "timestamp_requested": float(timestamp),
        "timestamp_actual": actual_t,
        "motion_frame_index": idx,
        "video_frame_index": timestamp_to_frame(t, seq.fps, seq.frame_count),
        "view": "sagittal OpenSim X–Y (forward–up)",
        "mime_type": "image/png",
        "image_base64": b64,
        "overlays": {
            "skeleton": True,
            "grf_arrows": seq.has_dynamics(),
            "joint_moment_disks": seq.has_dynamics(),
            "com": True,
            "com_trail_s": 0.8 if include_com_trail else 0.0,
            "contact_markers": True,
        },
        "note": (
            "Deterministic overlay of OpenSim body origins and estimated GRF/moments. "
            "Disk radius encodes |flexion moment|; red GRF arrows are estimated, not force plates. "
            "Do not read numerical values off the image — use dynamics tools."
        ),
        "warning": (seq.metadata.get("dynamics_metadata") or {}).get("warning"),
    }
    return GLOBAL_CACHE.set(cache_key, result)
