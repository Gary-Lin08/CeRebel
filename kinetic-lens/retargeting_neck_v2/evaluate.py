"""Old vs new head/neck error metrics and plots."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from .anatomical_landmarks import head_position, soma_body_rotations
from .config import HEAD_IN_TORSO, OUTPUTS_DIR, VISUAL_SCALE
from .rotations import geodesic_deg, matrix_from_quat_xyzw
from .soma_frames import SomaSequence


def _summarize(values_deg: np.ndarray) -> dict:
    v = np.asarray(values_deg, dtype=np.float64)
    return {
        "mean": round(float(np.mean(v)), 4),
        "median": round(float(np.median(v)), 4),
        "p95": round(float(np.percentile(v, 95)), 4),
        "max": round(float(np.max(v)), 4),
        "rmse": round(float(np.sqrt(np.mean(v**2))), 4),
    }


def _body_rot(frames: list[dict], name: str) -> np.ndarray:
    out = []
    for frame in frames:
        q = frame["bodies"][name]["q"]
        out.append(matrix_from_quat_xyzw(q))
    return np.stack(out)


def _body_pos(frames: list[dict], name: str) -> np.ndarray:
    return np.array([frame["bodies"][name]["p"] for frame in frames], dtype=np.float64)


def evaluate_pair(
    seq: SomaSequence,
    old_motion: dict,
    new_motion: dict,
) -> dict:
    n = min(
        seq.joints_opensim.shape[0],
        len(old_motion["frames"]),
        len(new_motion["frames"]),
    )
    soma_r = soma_body_rotations(seq)
    r_head_soma = soma_r["Head"][:n]
    r_chest_soma = soma_r["Chest"][:n]
    r_neck1_soma = soma_r["Neck1"][:n]
    p_head_soma = head_position(seq)[:n]

    old_frames = old_motion["frames"][:n]
    new_frames = new_motion["frames"][:n]
    r_torso_old = _body_rot(old_frames, "torso")
    r_torso_new = _body_rot(new_frames, "torso")
    r_head_new = _body_rot(new_frames, "head")
    r_neck_new = _body_rot(new_frames, "neck")
    p_head_new = _body_pos(new_frames, "head")
    p_torso_old = _body_pos(old_frames, "torso")
    head_offset = np.array(HEAD_IN_TORSO) * VISUAL_SCALE
    p_head_old = p_torso_old + np.einsum("tij,j->ti", r_torso_old, head_offset)

    # Remove rest-frame convention so geodesic error measures tracking, not axis naming.
    def align_to(src: np.ndarray, dst0: np.ndarray) -> np.ndarray:
        delta = src[0].T @ dst0
        return src @ delta

    r_head_soma_old = align_to(r_head_soma, r_torso_old[0])
    r_head_soma_new = align_to(r_head_soma, r_head_new[0])
    world_old = np.array([geodesic_deg(r_head_soma_old[t], r_torso_old[t]) for t in range(n)])
    world_new = np.array([geodesic_deg(r_head_soma_new[t], r_head_new[t]) for t in range(n)])

    rel_soma_lower = np.array(
        [r_chest_soma[t].T @ r_neck1_soma[t] for t in range(n)]
    )
    rel_soma_upper = np.array(
        [r_neck1_soma[t].T @ r_head_soma[t] for t in range(n)]
    )
    rel_new_lower = np.array([r_torso_new[t].T @ r_neck_new[t] for t in range(n)])
    rel_new_upper = np.array([r_neck_new[t].T @ r_head_new[t] for t in range(n)])
    rel_old_head = np.array([np.eye(3) for _ in range(n)])  # head welded to torso
    rel_soma_head = np.array([r_chest_soma[t].T @ r_head_soma[t] for t in range(n)])
    rel_new_head = np.array([r_torso_new[t].T @ r_head_new[t] for t in range(n)])

    lower_err = np.array([geodesic_deg(rel_soma_lower[t], rel_new_lower[t]) for t in range(n)])
    upper_err = np.array([geodesic_deg(rel_soma_upper[t], rel_new_upper[t]) for t in range(n)])
    rel_head_old = np.array([geodesic_deg(rel_soma_head[t], rel_old_head[t]) for t in range(n)])
    rel_head_new = np.array([geodesic_deg(rel_soma_head[t], rel_new_head[t]) for t in range(n)])

    pos_old = np.linalg.norm(p_head_old - p_head_soma, axis=1)
    pos_new = np.linalg.norm(p_head_new - p_head_soma, axis=1)

    old_rmse = float(np.sqrt(np.mean(world_old**2)))
    new_rmse = float(np.sqrt(np.mean(world_new**2)))
    improvement = 100.0 * (old_rmse - new_rmse) / max(old_rmse, 1e-9)

    coords = []
    for frame in new_frames:
        c = frame["coordinates"]
        coords.append(
            [
                math.degrees(c.get("neck_extension", 0.0)),
                math.degrees(c.get("neck_bending", 0.0)),
                math.degrees(c.get("neck_rotation", 0.0)),
                math.degrees(c.get("head_extension", 0.0)),
                math.degrees(c.get("head_bending", 0.0)),
                math.degrees(c.get("head_rotation", 0.0)),
            ]
        )
    coord_arr = np.array(coords)

    return {
        "n_frames": n,
        "head_orientation_world_old_deg": _summarize(world_old),
        "head_orientation_world_new_deg": _summarize(world_new),
        "head_orientation_error_deg": {
            "old_RMSE_deg": round(old_rmse, 4),
            "new_RMSE_deg": round(new_rmse, 4),
            "improvement_percent": round(improvement, 2),
            "old": _summarize(world_old),
            "new": _summarize(world_new),
            "note": "SO(3) geodesic after aligning frame 0; old uses torso as welded head",
        },
        "head_orientation_relative_to_torso": {
            "old": _summarize(rel_head_old),
            "new": _summarize(rel_head_new),
            "note": "Uncalibrated torso-relative geodesic; SOMA vs OpenSim body frames differ by a near-constant offset",
        },
        "neck_relative_orientation_error_deg": {
            "torso_to_neck": _summarize(lower_err),
            "neck_to_head": _summarize(upper_err),
        },
        "head_position_error_m": {
            "old": {
                "rmse": round(float(np.sqrt(np.mean(pos_old**2))), 4),
                "mean": round(float(np.mean(pos_old)), 4),
            },
            "new": {
                "rmse": round(float(np.sqrt(np.mean(pos_new**2))), 4),
                "mean": round(float(np.mean(pos_new)), 4),
            },
        },
        "series": {
            "world_old_deg": world_old.tolist(),
            "world_new_deg": world_new.tolist(),
            "rel_head_old_deg": rel_head_old.tolist(),
            "rel_head_new_deg": rel_head_new.tolist(),
            "pos_old_m": pos_old.tolist(),
            "pos_new_m": pos_new.tolist(),
            "neck_dof_deg": coord_arr.tolist(),
            "times": seq.times[:n].tolist(),
        },
    }


def write_plots(metrics: dict, plot_dir: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_dir.mkdir(parents=True, exist_ok=True)
    series = metrics["series"]
    t = np.array(series["times"])
    dof = np.array(series["neck_dof_deg"])
    written: list[Path] = []

    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    labels = [
        ("neck_extension", "Neck flexion/extension (deg)"),
        ("neck_bending", "Neck lateral bending (deg)"),
        ("neck_rotation", "Neck axial rotation (deg)"),
    ]
    for ax, col, (_, title) in zip(axes, range(3), labels):
        ax.plot(t, dof[:, col], color="#1f6feb")
        ax.set_ylabel(title)
        ax.grid(True, alpha=0.3)
    axes[-1].set_xlabel("time (s)")
    fig.tight_layout()
    p = plot_dir / "neck_coordinates.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    written.append(p)

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(t, series["world_old_deg"], label="old (head welded to torso)", color="#9b1c1c")
    ax.plot(t, series["world_new_deg"], label="new (articulated neck/head)", color="#1f6feb")
    ax.set_ylabel("Head orientation error vs SOMA (deg)")
    ax.set_xlabel("time (s)")
    ax.set_title("Head orientation geodesic error (frame-0 aligned)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    p = plot_dir / "head_orientation_error.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    written.append(p)

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(t, series["pos_old_m"], label="old", color="#9b1c1c")
    ax.plot(t, series["pos_new_m"], label="new", color="#1f6feb")
    ax.set_ylabel("Head position error (m)")
    ax.set_xlabel("time (s)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    p = plot_dir / "head_position_error.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    written.append(p)
    return written


def write_comparison_video(old_motion: dict, new_motion: dict, path: Path, fps: float) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FFMpegWriter, PillowWriter

    chains = [
        ("pelvis", "torso"),
        ("torso", "neck"),
        ("neck", "head"),
        ("torso", "humerus_l"),
        ("torso", "humerus_r"),
        ("pelvis", "femur_l"),
        ("pelvis", "femur_r"),
    ]
    n = min(len(old_motion["frames"]), len(new_motion["frames"]), 240)
    fig, axes = plt.subplots(1, 2, figsize=(10, 6), subplot_kw={"projection": "3d"})
    path.parent.mkdir(parents=True, exist_ok=True)

    def draw(ax, frame, title: str, extra_links: bool) -> None:
        ax.cla()
        ax.set_title(title)
        bodies = frame["bodies"]
        links = list(chains) if extra_links else [c for c in chains if "neck" not in c and c[1] != "head"]
        if not extra_links:
            links.append(("torso", "humerus_l"))
        for a, b in links:
            if a not in bodies or b not in bodies:
                continue
            pa, pb = bodies[a]["p"], bodies[b]["p"]
            ax.plot([pa[0], pb[0]], [pa[2], pb[2]], [pa[1], pb[1]], color="#333")
        for name, color in (("torso", "#888"), ("head", "#c45c26"), ("neck", "#1f6feb")):
            if name in bodies:
                p = bodies[name]["p"]
                ax.scatter([p[0]], [p[2]], [p[1]], c=color, s=30)
        ax.set_xlim(-0.8, 0.8)
        ax.set_ylim(-0.8, 0.8)
        ax.set_zlim(0.0, 2.0)
        ax.set_xlabel("X")
        ax.set_ylabel("Z")
        ax.view_init(elev=8, azim=-70)

    try:
        writer = FFMpegWriter(fps=min(fps, 30.0))
        suffix_ok = True
    except Exception:
        writer = PillowWriter(fps=12)
        suffix_ok = False
        path = path.with_suffix(".gif")

    with writer.saving(fig, str(path), dpi=100):
        for i in range(0, n, 2):
            draw(axes[0], old_motion["frames"][i], "OLD: head welded to torso", False)
            draw(axes[1], new_motion["frames"][i], "NEW: torso → neck → head", True)
            writer.grab_frame()
    plt.close(fig)
    return path
