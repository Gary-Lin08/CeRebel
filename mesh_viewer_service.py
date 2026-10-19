#!/usr/bin/env python3
"""Persistent GEM-X mesh visualizer for CeRebel sessions.

Watches ``$CEREBEL_DATA_DIR/latest_mesh.json`` and hot-swaps the viser scene to
the newest finished GEM-X run. Intended to be launched with the GEM-X venv::

    /root/GEM-X/.venv/bin/python /root/CerebelServer/mesh_viewer_service.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

# Ensure GEM-X is importable when this file lives outside the package.
GEMX_ROOT = Path(os.environ.get("CEREBEL_GEMX_ROOT", "/root/GEM-X")).resolve()
CEREBEL_ROOT = Path(os.environ.get("CEREBEL_ROOT", "/root/CerebelServer")).resolve()
if str(CEREBEL_ROOT) not in sys.path:
    sys.path.insert(0, str(CEREBEL_ROOT))
if str(GEMX_ROOT) not in sys.path:
    sys.path.insert(0, str(GEMX_ROOT))
os.chdir(GEMX_ROOT)

import functools

import torch

torch.load = functools.partial(torch.load, weights_only=False)

from gem.utils.vis.viser_viewer import MeshSequenceViewer, compute_meshes_from_hpe  # noqa: E402

try:
    from cerebel_config import fake_egocentric_enabled
except ImportError:  # pragma: no cover — when cwd is GEM-X only

    def fake_egocentric_enabled() -> bool:
        return os.environ.get("CEREBEL_FAKE_EGOCENTRIC", "0").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )


LOG = logging.getLogger("cerebel.mesh_viewer")

DATA_DIR = Path(os.environ.get("CEREBEL_DATA_DIR", "/root/CerebelServer/data"))
POINTER = DATA_DIR / "latest_mesh.json"
HOST = os.environ.get("CEREBEL_VISER_HOST", "0.0.0.0")
PORT = int(os.environ.get("CEREBEL_VISER_PORT", "8090"))
POLL_S = float(os.environ.get("CEREBEL_VISER_POLL_S", "1.0"))
DEVICE = os.environ.get("CEREBEL_GEMX_DEVICE", "cuda:0")
SOMA_ASSETS = os.environ.get(
    "CEREBEL_SOMA_ASSETS", str(GEMX_ROOT / "inputs" / "soma_assets")
)

def _y_up_to_z_up(verts):
    import numpy as np

    out = np.empty_like(verts)
    out[..., 0] = verts[..., 0]
    out[..., 1] = -verts[..., 2]
    out[..., 2] = verts[..., 1]
    return out


def _load_display_or_compute(hpe: Path, fps: float, session_id: str):
    """Prefer exported fake-ego pack; else fall back to global GEM-X meshes."""
    import numpy as np

    if fake_egocentric_enabled():
        try:
            from soma_display_export import compute_soma_display_meshes, display_mesh_paths

            session_dir = DATA_DIR / "sessions" / session_id
            bin_path, _meta = display_mesh_paths(session_dir)
            space = os.environ.get("CEREBEL_SOMA_DISPLAY_SPACE", "global")
            if fake_egocentric_enabled():
                space = "global"
            if not bin_path.is_file():
                from soma_display_export import export_soma_display_mesh

                export_soma_display_mesh(
                    hpe,
                    session_dir / "display",
                    fps=fps,
                    space=space,
                    device=DEVICE,
                    soma_assets=SOMA_ASSETS,
                )
            faces, verts = compute_soma_display_meshes(
                hpe, space=space, device=DEVICE, soma_assets=SOMA_ASSETS
            )
            # MeshSequenceViewer grid is Z-up; display meshes are Y-up.
            verts_z = _y_up_to_z_up(np.asarray(verts, dtype=np.float32))
            verts_z = verts_z.copy()
            verts_z[..., 2] -= float(verts_z[..., 2].min())
            return faces, verts_z, f"ego · session {session_id} ({verts.shape[0]} frames)"
        except Exception:
            LOG.exception("fake-egocentric mesh load failed; falling back to global")

    faces, verts_global = compute_meshes_from_hpe(
        hpe,
        device=DEVICE,
        soma_assets=SOMA_ASSETS,
    )
    return faces, verts_global, f"session {session_id} ({verts_global.shape[0]} frames)"


def _load_pointer() -> dict | None:
    if not POINTER.is_file():
        return None
    try:
        return json.loads(POINTER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("bad latest_mesh.json: %s", exc)
        return None


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("CEREBEL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    viewer = MeshSequenceViewer(host=HOST, port=PORT, fps=30.0)
    viewer.run(block=False)

    last_key: str | None = None
    LOG.info("watching %s for GEM-X results (viser :%s)", POINTER, PORT)

    while True:
        ptr = _load_pointer()
        if ptr and ptr.get("status") == "ready" and ptr.get("hpe_results"):
            hpe = Path(ptr["hpe_results"])
            key = f"{ptr.get('session_id')}:{hpe}:{ptr.get('updated_at')}"
            if key != last_key and hpe.is_file():
                try:
                    LOG.info("loading meshes for session %s", ptr.get("session_id"))
                    faces, verts, label = _load_display_or_compute(
                        hpe,
                        float(ptr.get("fps") or 30.0),
                        str(ptr.get("session_id") or ""),
                    )
                    viewer.update(
                        faces,
                        verts,
                        fps=float(ptr.get("fps") or 30.0),
                        label=label,
                    )
                    last_key = key
                except Exception:
                    LOG.exception("failed to load meshes from %s", hpe)
        elif ptr and ptr.get("status") == "running":
            ik = ptr.get("ik_status")
            if ik == "running":
                viewer._status.value = f"IK running… {ptr.get('session_id', '')}"  # noqa: SLF001
            else:
                viewer._status.value = f"GEM-X running… {ptr.get('session_id', '')}"  # noqa: SLF001
        elif ptr and ptr.get("status") == "ready" and ptr.get("ik_status") == "ready":
            kl_port = ptr.get("kinetic_lens_port", 8091)
            viewer._status.value = (  # noqa: SLF001
                f"session {ptr.get('session_id')} · OpenSim IK ready · "
                f"Kinetic Lens :{kl_port}"
            )
        elif ptr and ptr.get("status") == "error":
            viewer._status.value = f"GEM-X error: {ptr.get('error', 'unknown')}"  # noqa: SLF001
        time.sleep(POLL_S)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
