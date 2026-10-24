"""GEM-X body reconstruction as an autoscaling, per-second-billed GPU function on Modal.

Why: one always-on GPU caps throughput (one video at a time) and costs money while idle.
On Modal, each upload runs on its own GPU container, containers scale out under load and
back to zero when idle, and we pay only for the seconds a video is being processed.

Deploy (from a machine that has the GEM-X checkout, e.g. the current GPU server):

    pip install modal && modal setup                 # once; logs in to your Modal account
    modal volume create cerebel-gemx-weights          # once
    modal volume put cerebel-gemx-weights /root/GEM-X/inputs/pretrained /pretrained
    CEREBEL_GEMX_ROOT=/root/GEM-X modal deploy deploy/modal_gemx.py

Then on the web server set CEREBEL_GPU_BACKEND=modal (see gemx_pipeline.run_gemx_modal).
Knobs: CEREBEL_MODAL_GPU (default L40S), CEREBEL_MODAL_MIN_CONTAINERS (default 0; 1 keeps
one warm so the first user of the day doesn't wait for model loading),
CEREBEL_MODAL_MAX_CONTAINERS (default 10).

NOTE: written against the warm worker's interface but not yet deployed. The first deploy
may need the image's Python dependencies adjusted to match /root/GEM-X/.venv.
"""

from __future__ import annotations

import io
import os
import tarfile
import tempfile
import time
from pathlib import Path

import modal

GEMX_LOCAL = os.environ.get("CEREBEL_GEMX_ROOT", "/root/GEM-X")
GEMX_REMOTE = "/root/GEM-X"
GPU = os.environ.get("CEREBEL_MODAL_GPU", "L40S")

image = (
    modal.Image.from_registry("nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04", add_python="3.11")
    .apt_install("ffmpeg", "libgl1", "libglib2.0-0", "git")
    .pip_install_from_requirements(f"{GEMX_LOCAL}/requirements.txt")
    .add_local_dir(GEMX_LOCAL, GEMX_REMOTE, copy=True, ignore=["inputs/pretrained", "outputs", ".venv", ".git"])
)
weights = modal.Volume.from_name("cerebel-gemx-weights", create_if_missing=True)

app = modal.App("cerebel-gemx")


@app.cls(
    gpu=GPU,
    image=image,
    volumes={f"{GEMX_REMOTE}/inputs/pretrained": weights},
    timeout=20 * 60,
    scaledown_window=5 * 60,
    min_containers=int(os.environ.get("CEREBEL_MODAL_MIN_CONTAINERS", "0")),
    max_containers=int(os.environ.get("CEREBEL_MODAL_MAX_CONTAINERS", "10")),
)
class Gemx:
    @modal.enter()
    def load(self) -> None:
        import sys

        sys.path.insert(0, GEMX_REMOTE)
        os.chdir(GEMX_REMOTE)
        from scripts.demo.warm_soma_pipeline import WarmSomaPipeline

        ckpt = Path(GEMX_REMOTE) / "inputs" / "pretrained" / "gem_soma.ckpt"
        self.pipe = WarmSomaPipeline(ckpt_path=str(ckpt) if ckpt.is_file() else None)

    @modal.method()
    def run(self, session_id: str, video: bytes, suffix: str, fps: float) -> dict:
        """Process one clip; returns the output directory as a .tar.gz plus the worker's reply."""
        started = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / f"input{suffix or '.mp4'}"
            video_path.write_bytes(video)
            out_root = Path(tmp) / "out"
            result = self.pipe.run(
                session_id=session_id,
                output_root=str(out_root),
                video_path=str(video_path),
                jpeg_dir=None,
                static_cam=True,
                fps=float(fps or 30.0),
            )
            if not result.get("ok"):
                return {"ok": False, "error": result.get("error") or "GEM-X failed"}
            out_dir = Path(result["output_dir"])
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                tar.add(out_dir, arcname=".")
            hpe_rel = str(Path(result["hpe_results"]).relative_to(out_dir))
            return {
                "ok": True,
                "archive": buf.getvalue(),
                "hpe_rel": hpe_rel,
                "frames": result.get("frames"),
                "elapsed_s": round(time.time() - started, 2),
            }
