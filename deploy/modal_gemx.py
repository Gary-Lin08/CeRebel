"""GEM-X body reconstruction as an autoscaling, per-second-billed GPU function on Modal.

Why: one always-on GPU caps throughput (one video at a time) and costs money while idle.
On Modal, each upload runs on its own GPU container, containers scale out under load and
back to zero when idle, and we pay only for the seconds a video is being processed.

Deploy from the machine that has the GEM-X checkout (the current GPU server):

    bash deploy/modal_setup.sh        # login, upload weights once, deploy, smoke test

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

# Mirrors GEM-X's own Dockerfile (CUDA 12.6, Python 3.10, torch cu126, SOMA, detectron2, ONNX YOLOX).
image = (
    modal.Image.from_registry("nvidia/cuda:12.6.0-devel-ubuntu22.04", add_python="3.10")
    .apt_install(
        "git", "git-lfs", "wget", "curl", "xvfb", "ffmpeg", "build-essential",
        "libegl1-mesa-dev", "libgl1-mesa-glx", "libglib2.0-0",
    )
    .pip_install("uv")
    .run_commands("uv pip install --system torch torchvision --index-url https://download.pytorch.org/whl/cu126")
    # Code + SOMA LFS assets (third_party). Model weights live in a Volume, not the image.
    .add_local_dir(
        GEMX_LOCAL,
        GEMX_REMOTE,
        copy=True,
        ignore=["inputs/**", "outputs/**", ".venv/**", ".git/**", "**/__pycache__/**", "gem.egg-info/**"],
    )
    .run_commands(
        f"cd {GEMX_REMOTE} && uv pip install --system -e third_party/soma",
        f"cd {GEMX_REMOTE} && grep -vE '^(torch|torchvision)$' requirements.txt > /tmp/req.txt && uv pip install --system -r /tmp/req.txt",
        f"cd {GEMX_REMOTE} && uv pip install --system -e .",
        "uv pip install --system cloudpickle fvcore iopath pycocotools braceexpand roma 'setuptools<75' viser 'protobuf>=5.28.1,<6'",
        "FORCE_CUDA=1 TORCH_CUDA_ARCH_LIST='8.0;8.6;8.9;9.0' uv pip install --system "
        "'git+https://github.com/facebookresearch/detectron2.git@a1ce2f9' --no-build-isolation --no-deps",
        "uv pip install --system onnxruntime-gpu",
    )
    .env({"PYOPENGL_PLATFORM": "egl", "EGL_PLATFORM": "surfaceless", "HF_HOME": "/cache/hf", "TORCH_HOME": "/cache/torch"})
)
# inputs/ = checkpoints, pretrained, mhr_data, soma_data (~6.4 GB). Upload once with modal_setup.sh.
inputs_vol = modal.Volume.from_name("cerebel-gemx-inputs", create_if_missing=True)
# Hugging Face / torch hub downloads made at runtime are cached here across containers.
cache_vol = modal.Volume.from_name("cerebel-gemx-cache", create_if_missing=True)

app = modal.App("cerebel-gemx")


@app.cls(
    gpu=GPU,
    image=image,
    volumes={f"{GEMX_REMOTE}/inputs": inputs_vol, "/cache": cache_vol},
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


@app.local_entrypoint()
def smoke(video: str, fps: float = 30.0):
    """modal run deploy/modal_gemx.py::smoke --video path/to/clip.mp4"""
    data = Path(video).read_bytes()
    t0 = time.time()
    out = Gemx().run.remote("SMOKE-TEST", data, Path(video).suffix, fps)
    size = len(out.get("archive") or b"")
    print({k: v for k, v in out.items() if k != "archive"}, f"archive={size} bytes", f"wall={time.time() - t0:.1f}s")
