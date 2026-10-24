# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Headless-friendly mesh sequence viewer via viser (browser-based)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import torch

from gem.utils.geo_transform import apply_T_on_points, compute_T_ayfz2ay
from gem.utils.net_utils import to_cuda
from gem.utils.pylogger import Log
from gem.utils.soma_utils.soma_layer import SomaLayer


def _as_numpy(x) -> np.ndarray:
    if isinstance(x, torch.Tensor):
        return x.detach().float().cpu().numpy()
    return np.asarray(x, dtype=np.float32)


def _y_up_to_z_up(verts: np.ndarray) -> np.ndarray:
    """SOMA/Open3D use Y-up; viser’s default grid is Z-up (XY floor)."""
    out = np.empty_like(verts)
    out[..., 0] = verts[..., 0]
    out[..., 1] = -verts[..., 2]
    out[..., 2] = verts[..., 1]
    return out


def _normalize_global(verts: torch.Tensor, joints: torch.Tensor):
    """Put global mesh on the ground and face +Z (same convention as render_global_o3d)."""
    verts = verts.clone()
    joints = joints.clone()
    y_min = verts[:, :, 1].min()
    verts[:, :, 1] -= y_min
    joints[:, :, 1] -= y_min
    T_ay2ayfz = compute_T_ayfz2ay(joints[[0]], inverse=True)
    verts = apply_T_on_points(verts, T_ay2ayfz)
    joints = apply_T_on_points(joints, T_ay2ayfz)
    return verts, joints


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


def compute_meshes_from_hpe(
    hpe_results_path: str | Path,
    *,
    device: str = "cuda:0",
    soma_assets: str = "inputs/soma_assets",
):
    """Return (faces, verts_global) from a saved hpe_results.pt."""
    pred = torch.load(hpe_results_path, weights_only=False)
    soma = SomaLayer(
        data_root=soma_assets,
        low_lod=True,
        device=device,
        identity_model_type="mhr",
        mode="warp",
    )
    faces = _as_numpy(soma.faces).astype(np.int32)

    with torch.no_grad():
        out_glob = soma(**to_cuda(_get_body_params(pred, "body_params_global")))
        verts_glob, _ = _normalize_global(out_glob["vertices"], out_glob["joints"])

    verts_glob_np = _y_up_to_z_up(_as_numpy(verts_glob))
    verts_glob_np = verts_glob_np.copy()
    verts_glob_np[..., 2] -= float(verts_glob_np[..., 2].min())

    return faces, verts_glob_np


def compute_demo_meshes(cfg, device: str = "cuda:0"):
    """Return (faces, verts_global) numpy arrays for visualization."""
    return compute_meshes_from_hpe(cfg.paths.hpe_results, device=device)


class MeshSequenceViewer:
    """Long-lived viser UI that can hot-swap to a newer mesh sequence."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 8080,
        fps: float = 30.0,
    ) -> None:
        import viser

        self._lock = threading.RLock()
        self._fps = float(fps)
        self._faces: Optional[np.ndarray] = None
        self._verts_global: Optional[np.ndarray] = None
        self._num_frames = 0
        self._last_frame: Optional[int] = None
        self._session_label = "waiting for GEM-X…"
        self._stop = threading.Event()

        self.server = viser.ViserServer(host=host, port=port)
        self.server.scene.add_grid("/grid", width=12.0, height=12.0, cell_size=0.5)

        # Placeholder tetrahedron until the first real mesh arrives.
        stub_verts = np.array(
            [[0.0, 0.0, 0.0], [0.2, 0.0, 0.0], [0.1, 0.2, 0.0], [0.1, 0.05, 0.2]],
            dtype=np.float32,
        )
        stub_faces = np.array([[0, 1, 2], [0, 1, 3], [1, 2, 3], [0, 2, 3]], dtype=np.int32)
        self._mesh_global = self.server.scene.add_mesh_simple(
            "/global",
            vertices=stub_verts,
            faces=stub_faces,
            color=(80, 160, 220),
            opacity=1.0,
        )
        self._mesh_global.visible = False

        with self.server.gui.add_folder("Playback"):
            self._playing = self.server.gui.add_checkbox("Playing", initial_value=True)
            self._frame_slider = self.server.gui.add_slider(
                "Frame", min=0, max=1, step=1, initial_value=0
            )
            self._fps_slider = self.server.gui.add_slider(
                "FPS", min=1, max=60, step=1, initial_value=int(fps)
            )
            self._status = self.server.gui.add_text("Status", initial_value=self._session_label)

        Log.info(
            f"[Viser] Mesh viewer listening on http://0.0.0.0:{port} "
            f"(open this machine's IP:{port}). Waiting for GEM-X results."
        )

    def update(
        self,
        faces: np.ndarray,
        verts_global: np.ndarray,
        *,
        fps: float | None = None,
        label: str | None = None,
        verts_incam: np.ndarray | None = None,  # ignored; kept for call-site compat
    ) -> None:
        """Replace the displayed mesh sequence with the latest GEM-X global motion."""
        del verts_incam  # camera-perspective mesh is not shown
        faces = np.asarray(faces, dtype=np.int32)
        verts_global = np.asarray(verts_global, dtype=np.float32)
        if verts_global.ndim != 3:
            raise ValueError("Expected verts_global shaped (T, V, 3)")

        with self._lock:
            self._faces = faces
            self._verts_global = verts_global
            self._num_frames = int(verts_global.shape[0])
            self._last_frame = None
            if fps is not None:
                self._fps = float(fps)
                self._fps_slider.value = int(max(1, min(60, round(self._fps))))
            if label:
                self._session_label = label
                self._status.value = label

            self._mesh_global = self.server.scene.add_mesh_simple(
                "/global",
                vertices=verts_global[0],
                faces=faces,
                color=(80, 160, 220),
                opacity=1.0,
            )
            self._frame_slider.max = max(self._num_frames - 1, 0)
            self._frame_slider.value = 0
            self._apply_frame_unlocked(0)
            Log.info(
                f"[Viser] Updated mesh sequence ({self._num_frames} frames) — {self._session_label}"
            )

    def _apply_frame_unlocked(self, idx: int) -> None:
        if self._verts_global is None or self._num_frames < 1:
            return
        idx = int(np.clip(idx, 0, self._num_frames - 1))
        if idx == self._last_frame:
            return
        with self.server.atomic():
            self._mesh_global.vertices = self._verts_global[idx]
        self._last_frame = idx

    def _tick(self) -> None:
        with self._lock:
            if self._num_frames < 1:
                self._mesh_global.visible = False
                return

            self._mesh_global.visible = True

            if self._playing.value and self._num_frames > 1:
                nxt = (int(self._frame_slider.value) + 1) % self._num_frames
                self._frame_slider.value = nxt
                self._apply_frame_unlocked(nxt)
                sleep_s = 1.0 / max(float(self._fps_slider.value), 1.0)
            else:
                self._apply_frame_unlocked(int(self._frame_slider.value))
                sleep_s = 0.05
        time.sleep(sleep_s)

    def run(self, block: bool = True) -> None:
        if not block:
            thread = threading.Thread(target=self._loop, name="viser-playback", daemon=True)
            thread.start()
            return
        self._loop()

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._tick()
        except KeyboardInterrupt:
            Log.info("[Viser] Shutting down.")

    def stop(self) -> None:
        self._stop.set()


def launch_viser_demo(
    faces: np.ndarray,
    verts_global: np.ndarray,
    *,
    fps: float = 30.0,
    host: str = "0.0.0.0",
    port: int = 8080,
    block: bool = True,
    label: str | None = None,
    verts_incam: np.ndarray | None = None,  # ignored
):
    """Serve an interactive mesh playback UI over HTTP via viser (global mesh only)."""
    del verts_incam
    viewer = MeshSequenceViewer(host=host, port=port, fps=fps)
    viewer.update(faces, verts_global, fps=fps, label=label or "demo")
    if not block:
        viewer.run(block=False)
        return viewer
    viewer.run(block=True)
    return viewer
