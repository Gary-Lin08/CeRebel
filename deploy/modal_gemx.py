"""GEM-X body reconstruction as an autoscaling, per-second-billed GPU function on Modal.

The image is built entirely in Modal's (US) cloud: upstream NVlabs/GEM-X at the commit our
server runs, plus our local changes vendored in deploy/gemx_overlay. Model weights are
downloaded from Hugging Face (nvidia/GEM-X) straight into a Modal Volume by `prefetch`, so
nothing has to be uploaded from the GPU server in China.

Deploy from any machine that can reach Modal (e.g. the founder's Mac):

    bash deploy/modal_setup.sh            # or double-click deploy/modal_setup_mac.command

which runs:
    modal deploy deploy/modal_gemx.py                     # build image + deploy app
    modal run deploy/modal_gemx.py::prefetch              # weights -> Volume (once)
    modal run deploy/modal_gemx.py::smoke --video clip.mp4 [--gpu A100-40GB]

Then on the web server set CEREBEL_GPU_BACKEND=modal (see gemx_pipeline.run_gemx_modal,
which calls Cls.from_name("cerebel-gemx", "Gemx")().run.remote(session_id, video_bytes,
suffix, fps)).

Knobs (read at deploy time): CEREBEL_MODAL_GPU (default L40S), CEREBEL_MODAL_MIN_CONTAINERS
(default 0), CEREBEL_MODAL_MAX_CONTAINERS (default 10 = Modal Starter plan GPU limit).
"""

from __future__ import annotations

import io
import os
import tarfile
import tempfile
import time
from pathlib import Path

import modal

GEMX_REPO = "https://github.com/NVlabs/GEM-X.git"
GEMX_REMOTE = "/root/GEM-X"
VOL_INPUTS = "/vol/inputs"  # Volume mount; GEM-X/inputs/* are symlinks into it
GPU = os.environ.get("CEREBEL_MODAL_GPU", "L40S")
# Where GPU containers may run. Privacy policy: processing only in the US. Broad "us" costs 1.15x
# base price; a narrow region ("us-east") 1.75x. Comma-separated for several; empty means "us".
REGION = [r.strip() for r in (os.environ.get("CEREBEL_MODAL_REGION") or "us").split(",")
          if r.strip()] or ["us"]
OVERLAY = Path(__file__).resolve().parent / "gemx_overlay"


def _base_commit() -> str:
    f = OVERLAY / "BASE_COMMIT"
    return f.read_text().strip() if f.is_file() else "32992550dba114c62243fb55e361311972dce8f9"


BASE_COMMIT = _base_commit()
_WEIGHT_DIRS = ("checkpoints", "pretrained", "mhr_data", "soma_data")

# Mirrors the GPU server's working env (torch 2.4.0 + cu124, Python 3.10) and GEM-X's Dockerfile.
image = (
    modal.Image.from_registry("nvidia/cuda:12.4.1-devel-ubuntu22.04", add_python="3.10")
    .apt_install(
        "git", "git-lfs", "wget", "curl", "ffmpeg", "build-essential", "ninja-build",
        "libegl1-mesa-dev", "libgl1-mesa-glx", "libglib2.0-0", "libsm6", "libxext6",
    )
    .pip_install("uv")
    .run_commands(
        "uv pip install --system torch==2.4.0 torchvision==0.19.0 "
        "--index-url https://download.pytorch.org/whl/cu124",
    )
    # Upstream code at the exact commit the server runs; only the submodules we need
    # (soma-retargeter uses an ssh URL and is not used by the inference path).
    .run_commands(
        "git lfs install",
        f"git clone {GEMX_REPO} {GEMX_REMOTE}",
        f"cd {GEMX_REMOTE} && git checkout {BASE_COMMIT}",
        f"cd {GEMX_REMOTE} && git submodule update --init third_party/soma third_party/sam-3d-body",
        f"cd {GEMX_REMOTE}/third_party/soma && git lfs pull",
    )
    # Our local changes (patch to tracked files + new files).
    .add_local_dir(str(OVERLAY), "/overlay", copy=True)
    .run_commands(
        f"cd {GEMX_REMOTE} && git apply --whitespace=nowarn /overlay/gemx-local-changes.patch",
        f"cp -r /overlay/files/. {GEMX_REMOTE}/",
        f"cd {GEMX_REMOTE} && uv pip install --system -e third_party/soma",
        f"cd {GEMX_REMOTE} && grep -vE '^(torch|torchvision)\\s*$' requirements.txt > /tmp/req.txt "
        "&& uv pip install --system -r /tmp/req.txt",
        f"cd {GEMX_REMOTE} && uv pip install --system -e .",
        "uv pip install --system cloudpickle fvcore iopath pycocotools braceexpand roma "
        "'setuptools<75' viser 'protobuf>=5.28.1,<6' smplx 'numpy<2' wheel",
    )
    # detectron2 builds CUDA ops from source (needs torch + wheel at build time); own layer.
    .run_commands(
        "CC=gcc CXX=g++ MAX_JOBS=8 FORCE_CUDA=1 TORCH_CUDA_ARCH_LIST='8.0;8.6;8.9;9.0' uv pip install --system "
        "'git+https://github.com/facebookresearch/detectron2.git@a1ce2f9' --no-build-isolation --no-deps",
        "uv pip install --system onnxruntime-gpu==1.19.2",
        # inputs/: weights live in the Volume, SOMA assets come from the submodule (LFS).
        f"mkdir -p {GEMX_REMOTE}/inputs && "
        + " && ".join(f"ln -sfn {VOL_INPUTS}/{d} {GEMX_REMOTE}/inputs/{d}" for d in _WEIGHT_DIRS)
        + f" && ln -sfn {GEMX_REMOTE}/third_party/soma/assets {GEMX_REMOTE}/inputs/soma_assets",
    )
    .env({
        "PYOPENGL_PLATFORM": "egl",
        "EGL_PLATFORM": "surfaceless",
        "HF_HOME": "/cache/hf",
        "TORCH_HOME": "/cache/torch",
        "PYTHONPATH": GEMX_REMOTE,
    })
)

inputs_vol = modal.Volume.from_name("cerebel-gemx-inputs", create_if_missing=True)
# torch.hub (DINOv3 / ViTPose code), YOLOX weights and other runtime downloads are cached here.
cache_vol = modal.Volume.from_name("cerebel-gemx-cache", create_if_missing=True)
VOLUMES = {VOL_INPUTS: inputs_vol, "/cache": cache_vol}

app = modal.App("cerebel-gemx")


def _hf_secrets() -> list:
    """Optional Modal Secret 'huggingface' (HF_TOKEN), only needed if the HF repo becomes gated."""
    if os.environ.get("CEREBEL_MODAL_HF_SECRET"):
        return [modal.Secret.from_name(os.environ["CEREBEL_MODAL_HF_SECRET"])]
    return []


@app.function(image=image, volumes=VOLUMES, timeout=60 * 60, cpu=4, memory=8192, secrets=_hf_secrets())
def prefetch() -> dict:
    """Download all GEM-X weights from Hugging Face into the inputs Volume (idempotent)."""
    import sys

    os.chdir(GEMX_REMOTE)
    sys.path.insert(0, GEMX_REMOTE)
    for d in _WEIGHT_DIRS:
        Path(VOL_INPUTS, d).mkdir(parents=True, exist_ok=True)
    from huggingface_hub import hf_hub_download, list_repo_files

    # Where GEM-X expects each file (relative to inputs/), and an optional hint to pick among
    # same-named files. Paths inside the HF repo are discovered, since its layout can change.
    wanted = {
        "gem_soma.ckpt": ("pretrained", ""),
        "vitpose.pth": ("checkpoints/vitpose", ""),
        "sam3d_body.ckpt": ("checkpoints/sam-3d-body-dinov3", ""),
        "model_config.yaml": ("checkpoints/sam-3d-body-dinov3", "sam"),
        "mhr_model.pt": ("mhr_data", ""),
        "scale_mean.pth": ("soma_data", ""),
        "scale_comps.pth": ("soma_data", ""),
    }
    t0 = time.time()
    repo = "nvidia/GEM-X"
    # The repo's main branch was reorganised after the release our server runs; pick the newest
    # revision that still has every file we need (override with CEREBEL_GEMX_HF_REVISION).
    from huggingface_hub import list_repo_commits

    # 5b9a69d (2026-10-15) = last revision with the GEM-SOMA release files our server uses.
    revision = os.environ.get("CEREBEL_GEMX_HF_REVISION", "5b9a69d5ed4713ac4ba2a63b335e66db87279a3e") or None
    files = list_repo_files(repo, revision=revision)
    if revision is None and not all(any(f.rsplit("/", 1)[-1] == n for f in files) for n in wanted):
        best = None
        for c in list_repo_commits(repo):
            fs = list_repo_files(repo, revision=c.commit_id)
            have = sum(any(f.rsplit("/", 1)[-1] == n for f in fs) for n in wanted)
            print(f"revision {c.commit_id[:10]} {c.created_at} {c.title!r}: {have}/{len(wanted)}", flush=True)
            if best is None or have > best[0]:
                best = (have, c.commit_id, fs)
            if have == len(wanted):
                break
        _, revision, files = best
    print("HF revision:", revision, "files:", files, flush=True)
    done, missing = {}, []
    for name, (sub, hint) in wanted.items():
        target = Path(VOL_INPUTS, sub, name)
        marker = target.with_name(target.name + ".rev")
        if target.is_file() and target.stat().st_size > 0 and marker.is_file() and marker.read_text() == str(revision):
            done[name] = f"cached {target}"
            continue
        cands = [f for f in files if f.rsplit("/", 1)[-1] == name]
        if hint:
            cands = sorted(cands, key=lambda f: (hint not in f.lower(), len(f)))
        else:
            cands = sorted(cands, key=len)
        if not cands:
            missing.append(name)
            continue
        src = hf_hub_download(repo_id=repo, filename=cands[0], revision=revision, local_dir=f"{VOL_INPUTS}/_hf")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, target)
        marker.write_text(str(revision))
        done[name] = f"{cands[0]} -> {target}"
        print("downloaded", done[name], flush=True)
    if missing:
        print("MISSING in HF repo:", missing, flush=True)
    inputs_vol.commit()
    extra = {}
    try:  # YOLOX-X person detector weights (openmmlab) -> /cache/torch
        from gem.utils.yolox_torch import download_yolox_x_checkpoint

        extra["yolox"] = str(download_yolox_x_checkpoint())
    except Exception as exc:  # noqa: BLE001
        extra["yolox_error"] = repr(exc)
    try:  # DINOv3 model code used by SAM-3D-Body / ViTPose loaders (torch.hub, no weights)
        import torch

        torch.hub.load("facebookresearch/dinov3", "dinov3_vits16", source="github",
                       pretrained=False, skip_validation=True, trust_repo=True)
        extra["dinov3_hub"] = "ok"
    except Exception as exc:  # noqa: BLE001
        extra["dinov3_error"] = repr(exc)
    cache_vol.commit()
    sizes = {}
    for d in _WEIGHT_DIRS:
        total = sum(p.stat().st_size for p in Path(VOL_INPUTS, d).rglob("*") if p.is_file())
        sizes[d] = f"{total / 1e9:.2f} GB"
    soma_assets = Path(GEMX_REMOTE, "inputs", "soma_assets")
    result = {
        "files": done,
        "missing": missing,
        "hf_revision": revision,
        "extra": extra,
        "sizes": sizes,
        "soma_assets_entries": sorted(p.name for p in soma_assets.iterdir()) if soma_assets.exists() else [],
        "elapsed_s": round(time.time() - t0, 1),
    }
    print("PREFETCH RESULT", result, flush=True)
    return result


# CEREBEL_MODAL_PREFETCH_ONLY=1 leaves the GPU class out, so `modal run ...::prefetch` works on a
# workspace that has no payment method yet (Modal requires one for any GPU function).
if os.environ.get("CEREBEL_MODAL_PREFETCH_ONLY") != "1":

    @app.cls(
        gpu=GPU,
        region=REGION,
        image=image,
        volumes=VOLUMES,
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
            t0 = time.time()
            from scripts.demo.warm_soma_pipeline import WarmSomaPipeline

            ckpt = Path(GEMX_REMOTE) / "inputs" / "pretrained" / "gem_soma.ckpt"
            self.pipe = WarmSomaPipeline(ckpt_path=str(ckpt) if ckpt.is_file() else None)
            self.load_s = round(time.time() - t0, 1)
            try:
                cache_vol.commit()  # keep anything downloaded during the first load
            except Exception:  # noqa: BLE001
                pass

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
                    "model_load_s": getattr(self, "load_s", None),
                }


@app.local_entrypoint()
def smoke(video: str, fps: float = 30.0, gpu: str = "", runs: int = 2):
    """modal run deploy/modal_gemx.py::smoke --video clip.mp4 [--gpu A100-40GB] [--runs 2]

    Calls the *deployed* app (what production uses). First call = cold start, second = warm.
    """
    data = Path(video).read_bytes()
    cls = modal.Cls.from_name("cerebel-gemx", "Gemx")
    if gpu:
        cls = cls.with_options(gpu=gpu)
    obj = cls()
    for i in range(runs):
        t0 = time.time()
        out = obj.run.remote(f"SMOKE-{i}", data, Path(video).suffix, fps)
        size = len(out.get("archive") or b"")
        info = {k: v for k, v in out.items() if k != "archive"}
        print(f"SMOKE gpu={gpu or GPU} run={i} wall={time.time() - t0:.1f}s archive={size}B {info}", flush=True)
