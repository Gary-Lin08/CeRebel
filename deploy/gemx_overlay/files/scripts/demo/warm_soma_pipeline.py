# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E402, I001
"""Warm GEM-X SOMA pipeline: preload models once, run inference on demand."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
os.environ.setdefault("EGL_PLATFORM", "surfaceless")

import torch

torch.load = functools.partial(torch.load, weights_only=False)

import hydra
import numpy as np
from hydra import compose, initialize_config_dir
from hydra.core.global_hydra import GlobalHydra

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gem.utils.cam_utils import estimate_K, get_a_pred_cam
from gem.utils.geo_transform import (
    compute_cam_angvel,
    compute_cam_tvel,
    get_bbx_xys_from_xyxy,
)
from gem.utils.kp2d_utils import smooth_bbx_xyxy
from gem.utils.net_utils import detach_to_cpu
from gem.utils.pylogger import Log
from gem.utils.sam3db_extractor import SAM3DBExtractor
from gem.utils.video_io_utils import read_images_np, read_video_np
from gem.utils.vitpose_extractor import VitPoseExtractor
from gem.utils.yolox_detector import detect_and_track
from gem.utils.yolox_torch import YOLOXTorchDetector

# A100-40G oriented defaults (overridable). VitPose flip_test doubles effective batch.
_DEFAULT_VITPOSE_BATCH = int(os.environ.get("CEREBEL_VITPOSE_BATCH", "96"))
_DEFAULT_SAM3D_BATCH = int(os.environ.get("CEREBEL_SAM3D_BATCH", "48"))
_DEFAULT_YOLO_BATCH = int(os.environ.get("CEREBEL_YOLO_BATCH", "16"))
_USE_BYTETRACK = os.environ.get("CEREBEL_USE_BYTETRACK", "0").lower() not in (
    "0",
    "false",
    "no",
)
_VITPOSE_FLIP = os.environ.get("CEREBEL_VITPOSE_FLIP", "1").lower() not in (
    "0",
    "false",
    "no",
)
_AUTO_TUNE = os.environ.get("CEREBEL_BATCH_AUTOTUNE", "1").lower() not in (
    "0",
    "false",
    "no",
)


def _compose_demo_cfg(overrides: list[str]):
    if GlobalHydra.instance().is_initialized():
        GlobalHydra.instance().clear()
    cfg_dir = PROJECT_ROOT / "configs"
    with initialize_config_dir(version_base="1.3", config_dir=str(cfg_dir)):
        return compose(config_name="demo_soma", overrides=overrides)


def _is_oom(exc: BaseException) -> bool:
    if isinstance(exc, torch.cuda.OutOfMemoryError):
        return True
    msg = str(exc).lower()
    return "out of memory" in msg or "cuda oom" in msg


def _with_batch_backoff(label: str, start_bs: int, fn):
    """Call fn(batch_size); on CUDA OOM halve batch until it fits."""
    bs = max(1, int(start_bs))
    last_exc: BaseException | None = None
    while bs >= 1:
        try:
            return fn(bs), bs
        except Exception as exc:
            if not _is_oom(exc):
                raise
            last_exc = exc
            Log.warning(f"[Warm] {label} OOM at batch={bs}; retrying with {bs // 2}")
            torch.cuda.empty_cache()
            bs = bs // 2
    raise RuntimeError(f"{label} failed even at batch=1") from last_exc


def _probe_max_batch(label: str, trial_fn, start_bs: int, max_bs: int = 256) -> int:
    """Binary-search the largest power-of-two-ish batch that fits in VRAM."""
    lo, hi = 1, max(1, min(int(start_bs), int(max_bs)))
    # Grow until OOM or max_bs.
    bs = 8
    best = 1
    while bs <= max_bs:
        try:
            torch.cuda.empty_cache()
            trial_fn(bs)
            best = bs
            if bs >= max_bs:
                break
            bs = min(max_bs, bs * 2)
        except Exception as exc:
            if not _is_oom(exc):
                raise
            torch.cuda.empty_cache()
            Log.info(f"[Warm] {label} probe OOM at batch={bs}")
            hi = bs - 1
            break
    else:
        Log.info(f"[Warm] {label} probe ok up to batch={best}")
        return best

    # Binary search between best and hi.
    lo = best
    while lo < hi:
        mid = max(lo + 1, (lo + hi + 1) // 2)
        try:
            torch.cuda.empty_cache()
            trial_fn(mid)
            lo = mid
            best = mid
        except Exception as exc:
            if not _is_oom(exc):
                raise
            torch.cuda.empty_cache()
            hi = mid - 1
    Log.info(f"[Warm] {label} tuned batch={best}")
    return best


def _resolve_ckpt(ckpt_path: str | None) -> str:
    if ckpt_path and Path(ckpt_path).is_file():
        return ckpt_path
    local = PROJECT_ROOT / "inputs" / "pretrained" / "gem_soma.ckpt"
    if local.is_file():
        return str(local)
    from gem.utils.hf_utils import download_checkpoint

    Log.info("[Warm] Downloading GEM checkpoint from HuggingFace…")
    return download_checkpoint()


def _load_video_resampled(video_path: str, fps: float) -> np.ndarray:
    """Decode video after the same ``fps=`` filter used for session JPEG extract.

    Native ``read_video_np`` keeps container frame rate; without resampling a
    60 fps phone clip would produce ~2× frames vs ``header.target_fps``.
    """
    fps = float(fps) if fps and float(fps) > 0 else 30.0
    td = tempfile.mkdtemp(prefix="gemx_fps_")
    try:
        out = Path(td) / "resampled.mp4"
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            video_path,
            "-vf",
            f"fps={fps}",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0 or not out.is_file():
            detail = (proc.stderr or proc.stdout or "ffmpeg fps resample failed").strip()[-800:]
            raise RuntimeError(detail)
        Log.info(f"[Warm] Resampled video to {fps:g} fps → loading {out}")
        return read_video_np(str(out))
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _load_frames(
    video_path: str | None,
    jpeg_dir: str | None,
    fps: float = 30.0,
) -> np.ndarray:
    if jpeg_dir:
        paths = sorted(Path(jpeg_dir).glob("*.jpg"))
        if not paths:
            raise FileNotFoundError(f"no JPEGs in {jpeg_dir}")
        Log.info(f"[Warm] Loading {len(paths)} JPEGs from {jpeg_dir}")
        return read_images_np(paths)
    if not video_path:
        raise ValueError("need video_path or jpeg_dir")
    Log.info(f"[Warm] Loading video {video_path} at target fps={fps}")
    return _load_video_resampled(video_path, fps)


class WarmSomaPipeline:
    """Keeps YOLOX / ViTPose / SAM3D / GEM resident on GPU for fast repeat runs."""

    def __init__(
        self,
        *,
        ckpt_path: str | None = None,
        exp: str = "gem_soma_regression",
        device: str = "cuda:0",
        sam3d_ckpt_path: str | None = None,
        sam3d_mhr_path: str | None = None,
    ) -> None:
        self.device = device
        self.exp = exp
        t0 = time.time()
        torch.backends.cudnn.benchmark = True
        try:
            torch.set_float32_matmul_precision("high")
        except Exception:
            pass

        Log.info("[Warm] Loading YOLOX (PyTorch checkpoint, batched)…")
        self.yolox = YOLOXTorchDetector(device=device)
        self.yolo_batch = _DEFAULT_YOLO_BATCH
        self.use_bytetrack = _USE_BYTETRACK
        Log.info(
            f"[Warm] YOLO batch={self.yolo_batch} bytetrack={self.use_bytetrack}"
        )
        Log.info("[Warm] Loading ViTPose…")
        self.vitpose = VitPoseExtractor(device=device, pose_type="soma")
        self.vitpose.flip_test = _VITPOSE_FLIP
        Log.info(f"[Warm] ViTPose flip_test={self.vitpose.flip_test}")
        Log.info("[Warm] Loading SAM3D Body…")
        self.sam3d = SAM3DBExtractor(
            checkpoint_path=sam3d_ckpt_path,
            mhr_path=sam3d_mhr_path,
            device=device,
        )
        Log.info("[Warm] Loading GEM model…")
        # Placeholder video fields required by the compose schema.
        self._base_overrides = [
            f"exp={exp}",
            "video_name=_warm_placeholder_",
            "video_path=/dev/null",
            "output_root=outputs/cerebel",
            "static_cam=true",
            "verbose=false",
            "render_mhr=false",
            "use_wandb=false",
            "task=test",
        ]
        cfg = _compose_demo_cfg(self._base_overrides)
        self.model = hydra.utils.instantiate(cfg.model, _recursive_=False)
        resolved = _resolve_ckpt(ckpt_path or cfg.get("ckpt_path"))
        self.model.load_pretrained_model(resolved)
        self.model = self.model.eval().cuda()
        self.ckpt_path = resolved

        self.vitpose_batch = _DEFAULT_VITPOSE_BATCH
        self.sam3d_batch = _DEFAULT_SAM3D_BATCH
        if _AUTO_TUNE:
            self._autotune_batches()
        free, total = torch.cuda.mem_get_info()
        Log.info(
            f"[Warm] All models ready in {time.time() - t0:.1f}s "
            f"(ckpt={resolved}, yolo_bs={self.yolo_batch}, "
            f"vitpose_bs={self.vitpose_batch}, "
            f"sam3d_bs={self.sam3d_batch}, bytetrack={self.use_bytetrack}, "
            f"vram_free={free/1024**3:.1f}/{total/1024**3:.1f} GiB)"
        )

    def _autotune_batches(self) -> None:
        """Push YOLO / VitPose / SAM3D batch sizes up to what VRAM can hold."""
        H, W = 480, 640
        bbx = torch.tensor([[W * 0.5, H * 0.5, min(H, W) * 0.7]], dtype=torch.float32)

        def yolo_trial(bs: int) -> None:
            frames = [
                np.random.randint(0, 255, (H, W, 3), dtype=np.uint8) for _ in range(bs)
            ]
            self.yolox.detect_batch(frames)
            torch.cuda.synchronize()

        def vit_trial(bs: int) -> None:
            frames = np.random.randint(0, 255, (bs, H, W, 3), dtype=np.uint8)
            bbx_xys = bbx.repeat(bs, 1)
            self.vitpose.extract(frames, bbx_xys, img_ds=1.0, path_type="np", batch_size=bs)
            torch.cuda.synchronize()

        def sam_trial(bs: int) -> None:
            frames = np.random.randint(0, 255, (bs, H, W, 3), dtype=np.uint8)
            bbx_xys = bbx.repeat(bs, 1)
            self.sam3d.extract_features(frames, bbx_xys, batch_size=bs)
            torch.cuda.synchronize()

        Log.info("[Warm] Autotuning YOLO batch size…")
        self.yolo_batch = _probe_max_batch(
            "YOLO", yolo_trial, start_bs=_DEFAULT_YOLO_BATCH, max_bs=64
        )
        Log.info("[Warm] Autotuning VitPose batch size…")
        self.vitpose_batch = _probe_max_batch(
            "ViTPose", vit_trial, start_bs=_DEFAULT_VITPOSE_BATCH, max_bs=256
        )
        Log.info("[Warm] Autotuning SAM3D batch size…")
        self.sam3d_batch = _probe_max_batch(
            "SAM3D", sam_trial, start_bs=_DEFAULT_SAM3D_BATCH, max_bs=128
        )
        torch.cuda.empty_cache()

    def _build_cfg(self, *, video_name: str, output_root: str, static_cam: bool):
        overrides = [
            f"exp={self.exp}",
            f"video_name={video_name}",
            "video_path=/dev/null",
            f"output_root={output_root}",
            f"static_cam={str(static_cam).lower()}",
            "verbose=false",
            "render_mhr=false",
            "use_wandb=false",
            "task=test",
            f"ckpt_path={self.ckpt_path}",
        ]
        cfg = _compose_demo_cfg(overrides)
        Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
        Path(cfg.preprocess_dir).mkdir(parents=True, exist_ok=True)
        return cfg

    @torch.no_grad()
    def run(
        self,
        *,
        session_id: str,
        output_root: str,
        video_path: str | None = None,
        jpeg_dir: str | None = None,
        static_cam: bool = True,
        fps: float = 30.0,
    ) -> dict:
        """Run full HPE on one capture. Does NOT render intermediate overlay videos."""
        t0 = time.time()
        cfg = self._build_cfg(
            video_name=session_id, output_root=output_root, static_cam=static_cam
        )
        frames = _load_frames(video_path, jpeg_dir, fps=fps)
        L, H, W = frames.shape[0], frames.shape[1], frames.shape[2]
        Log.info(f"[Warm] session={session_id} frames={L} size={W}x{H}")

        # --- detect ---
        t1 = time.time()
        bbx_xyxy_np, _ = detect_and_track(
            frames,
            self.yolox,
            use_bytetrack=self.use_bytetrack,
            batch_size=self.yolo_batch,
        )
        bbx_xyxy = torch.from_numpy(bbx_xyxy_np).float()
        bbx_xyxy = smooth_bbx_xyxy(bbx_xyxy, window=5)
        bbx_xyxy[:, [0, 2]] = bbx_xyxy[:, [0, 2]].clamp(0, W - 1)
        bbx_xyxy[:, [1, 3]] = bbx_xyxy[:, [1, 3]].clamp(0, H - 1)
        bbx_xys = get_bbx_xys_from_xyxy(bbx_xyxy, base_enlarge=1.2).float()
        torch.save({"bbx_xyxy": bbx_xyxy, "bbx_xys": bbx_xys}, cfg.paths.bbx)
        Log.info(
            f"[Warm] detect {time.time() - t1:.2f}s "
            f"(yolo_bs={self.yolo_batch}, bytetrack={self.use_bytetrack})"
        )

        # --- vitpose ---
        t1 = time.time()
        vitpose, used_vbs = _with_batch_backoff(
            "ViTPose",
            self.vitpose_batch,
            lambda bs: self.vitpose.extract(
                frames, bbx_xys, img_ds=1.0, path_type="np", batch_size=bs
            ),
        )
        self.vitpose_batch = used_vbs
        torch.save(vitpose, cfg.paths.vitpose)
        Log.info(f"[Warm] vitpose {time.time() - t1:.2f}s (batch={used_vbs})")

        # --- static cam ---
        eye = torch.eye(4).unsqueeze(0).repeat(L, 1, 1).numpy()
        torch.save(eye, cfg.paths.slam)

        # --- sam3d features ---
        t1 = time.time()
        sam3d_results, used_sbs = _with_batch_backoff(
            "SAM3D",
            self.sam3d_batch,
            lambda bs: self.sam3d.extract_features(frames, bbx_xys, batch_size=bs),
        )
        self.sam3d_batch = used_sbs
        K_fullimg = estimate_K(W, H).repeat(L, 1, 1)
        pred_cam = get_a_pred_cam(sam3d_results["transls"], bbx_xys, K_fullimg)
        vit_features = {
            "pose_tokens": sam3d_results["pose_tokens"],
            "pred_cam": pred_cam,
        }
        torch.save(vit_features, cfg.paths.vit_features)
        Log.info(f"[Warm] sam3d {time.time() - t1:.2f}s (batch={used_sbs})")

        # --- gem predict ---
        t1 = time.time()
        if isinstance(vitpose, tuple):
            vitpose = vitpose[0]
        R_w2c = torch.eye(3).repeat(L, 1, 1)
        t_w2c = torch.zeros(L, 3)
        T_w2c = torch.eye(4).unsqueeze(0).repeat(L, 1, 1)
        data = {
            "meta": [{"vid": session_id}],
            "length": torch.tensor(L),
            "bbx_xys": bbx_xys.clone(),
            "kp2d": vitpose,
            "K_fullimg": K_fullimg,
            "cam_angvel": compute_cam_angvel(R_w2c),
            "cam_tvel": compute_cam_tvel(t_w2c),
            "R_w2c": R_w2c,
            "T_w2c": T_w2c,
            "f_imgseq": vit_features["pose_tokens"],
            "noisy_pred_cam": vit_features.get("pred_cam", None),
            "has_text": torch.tensor([False]),
            "mask": {
                "valid": torch.ones(L).bool(),
                "has_img_mask": torch.ones(L).bool(),
                "has_2d_mask": torch.ones(L).bool(),
                "has_cam_mask": torch.ones(L).bool(),
                "has_audio_mask": torch.zeros(L).bool(),
                "has_music_mask": torch.zeros(L).bool(),
            },
        }
        pred = self.model.predict(data, static_cam=static_cam, postproc=True)
        torch.save(detach_to_cpu(pred), cfg.paths.hpe_results)
        Log.info(f"[Warm] gem {time.time() - t1:.2f}s")

        total = time.time() - t0
        Log.info(
            f"[Warm] Done session={session_id} in {total:.1f}s "
            f"({L / max(total, 1e-6):.1f} fps effective) → {cfg.paths.hpe_results}"
        )
        return {
            "ok": True,
            "session_id": session_id,
            "output_dir": str(cfg.output_dir),
            "hpe_results": str(cfg.paths.hpe_results),
            "fps": float(fps),
            "frames": int(L),
            "elapsed_s": total,
        }
