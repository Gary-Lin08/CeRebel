# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pure-PyTorch YOLOX-X (MMDet-compatible) for batched person detection.

Loads the OpenMMLab HumanArt YOLOX-X checkpoint without requiring mmcv/mmdet.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision.ops import nms

YOLOX_X_PTH_URL = (
    "https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/"
    "yolox_x_8xb8-300e_humanart-a39d44ed.pth"
)
_CACHE_DIR = (
    Path(os.getenv("TORCH_HOME", os.path.expanduser("~/.cache/rtmlib"))) / "hub" / "checkpoints"
)


def download_yolox_x_checkpoint(url: str = YOLOX_X_PTH_URL) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    name = os.path.basename(urlparse(url).path)
    dest = _CACHE_DIR / name
    if dest.is_file() and dest.stat().st_size > 1_000_000:
        return dest
    # Prefer already-downloaded copy from /tmp if present.
    tmp = Path("/tmp") / name.replace("yolox_x_8xb8-300e_humanart-a39d44ed.pth", "yolox_x_humanart.pth")
    if tmp.is_file():
        dest.write_bytes(tmp.read_bytes())
        return dest
    sys.stderr.write(f'Downloading YOLOX checkpoint: "{url}" → {dest}\n')
    req = Request(url, headers={"User-Agent": "gem-yolox"})
    # Write to a temp name and rename only once complete: a cut-off download must never be
    # left under the real name (the size check above would then accept it forever).
    part = dest.with_name(dest.name + ".part")
    with urlopen(req) as resp, open(part, "wb") as f:
        expected = int(resp.headers.get("Content-Length") or 0)
        while True:
            buf = resp.read(1 << 20)
            if not buf:
                break
            f.write(buf)
    got = part.stat().st_size
    if expected and got != expected:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"YOLOX download incomplete: {got} of {expected} bytes")
    part.replace(dest)
    return dest


class ConvModule(nn.Module):
    """MMDet/MMCV ConvModule naming: .conv / .bn / .activate (SiLU)."""

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size: int,
        stride: int = 1,
        padding: int | None = None,
        bias: bool | str = "auto",
    ):
        super().__init__()
        if padding is None:
            padding = (kernel_size - 1) // 2
        if bias == "auto":
            bias = False
        self.conv = nn.Conv2d(
            in_channels, out_channels, kernel_size, stride=stride, padding=padding, bias=bias
        )
        self.bn = nn.BatchNorm2d(out_channels, eps=0.001, momentum=0.03)
        self.activate = nn.SiLU(inplace=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.activate(self.bn(self.conv(x)))


class Focus(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, kernel_size: int = 1):
        super().__init__()
        self.conv = ConvModule(in_channels * 4, out_channels, kernel_size)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        patch_top_left = x[..., ::2, ::2]
        patch_top_right = x[..., ::2, 1::2]
        patch_bot_left = x[..., 1::2, ::2]
        patch_bot_right = x[..., 1::2, 1::2]
        x = torch.cat((patch_top_left, patch_bot_left, patch_top_right, patch_bot_right), dim=1)
        return self.conv(x)


class SPPBottleneck(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, kernel_sizes=(5, 9, 13)):
        super().__init__()
        mid = in_channels // 2
        self.conv1 = ConvModule(in_channels, mid, 1)
        self.poolings = nn.ModuleList(
            [nn.MaxPool2d(kernel_size=ks, stride=1, padding=ks // 2) for ks in kernel_sizes]
        )
        self.conv2 = ConvModule(mid * (len(kernel_sizes) + 1), out_channels, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.conv1(x)
        x = torch.cat([x] + [p(x) for p in self.poolings], dim=1)
        return self.conv2(x)


class DarknetBottleneck(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        expansion: float = 0.5,
        add_identity: bool = True,
    ):
        super().__init__()
        hidden = int(out_channels * expansion)
        self.conv1 = ConvModule(in_channels, hidden, 1)
        self.conv2 = ConvModule(hidden, out_channels, 3, padding=1)
        self.add_identity = add_identity and in_channels == out_channels

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.conv2(self.conv1(x))
        return out + x if self.add_identity else out


class CSPLayer(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        num_blocks: int = 1,
        add_identity: bool = True,
        expand_ratio: float = 0.5,
    ):
        super().__init__()
        mid = int(out_channels * expand_ratio)
        self.main_conv = ConvModule(in_channels, mid, 1)
        self.short_conv = ConvModule(in_channels, mid, 1)
        self.final_conv = ConvModule(2 * mid, out_channels, 1)
        self.blocks = nn.Sequential(
            *[
                DarknetBottleneck(mid, mid, expansion=1.0, add_identity=add_identity)
                for _ in range(num_blocks)
            ]
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x_short = self.short_conv(x)
        x_main = self.blocks(self.main_conv(x))
        return self.final_conv(torch.cat((x_main, x_short), dim=1))


class CSPDarknet(nn.Module):
    arch_settings = {
        "P5": [
            [64, 128, 3, True, False],
            [128, 256, 9, True, False],
            [256, 512, 9, True, False],
            [512, 1024, 3, False, True],
        ],
    }

    def __init__(
        self,
        deepen_factor: float = 1.33,
        widen_factor: float = 1.25,
        out_indices: Sequence[int] = (2, 3, 4),
        spp_kernal_sizes=(5, 9, 13),
    ):
        super().__init__()
        arch = self.arch_settings["P5"]
        self.out_indices = out_indices
        self.stem = Focus(3, int(arch[0][0] * widen_factor), kernel_size=3)
        self.layers = ["stem"]
        for i, (in_c, out_c, n_blocks, add_id, use_spp) in enumerate(arch):
            in_c = int(in_c * widen_factor)
            out_c = int(out_c * widen_factor)
            n_blocks = max(round(n_blocks * deepen_factor), 1)
            stage: list[nn.Module] = [ConvModule(in_c, out_c, 3, stride=2, padding=1)]
            if use_spp:
                stage.append(SPPBottleneck(out_c, out_c, kernel_sizes=spp_kernal_sizes))
            stage.append(
                CSPLayer(out_c, out_c, num_blocks=n_blocks, add_identity=add_id)
            )
            self.add_module(f"stage{i + 1}", nn.Sequential(*stage))
            self.layers.append(f"stage{i + 1}")

    def forward(self, x: torch.Tensor):
        outs = []
        for i, name in enumerate(self.layers):
            x = getattr(self, name)(x)
            if i in self.out_indices:
                outs.append(x)
        return tuple(outs)


class YOLOXPAFPN(nn.Module):
    def __init__(
        self,
        in_channels: Sequence[int] = (320, 640, 1280),
        out_channels: int = 320,
        num_csp_blocks: int = 4,
    ):
        super().__init__()
        self.in_channels = list(in_channels)
        self.upsample = nn.Upsample(scale_factor=2, mode="nearest")
        self.reduce_layers = nn.ModuleList()
        self.top_down_blocks = nn.ModuleList()
        for idx in range(len(in_channels) - 1, 0, -1):
            self.reduce_layers.append(ConvModule(in_channels[idx], in_channels[idx - 1], 1))
            self.top_down_blocks.append(
                CSPLayer(
                    in_channels[idx - 1] * 2,
                    in_channels[idx - 1],
                    num_blocks=num_csp_blocks,
                    add_identity=False,
                )
            )
        self.downsamples = nn.ModuleList()
        self.bottom_up_blocks = nn.ModuleList()
        for idx in range(len(in_channels) - 1):
            self.downsamples.append(ConvModule(in_channels[idx], in_channels[idx], 3, stride=2, padding=1))
            self.bottom_up_blocks.append(
                CSPLayer(
                    in_channels[idx] * 2,
                    in_channels[idx + 1],
                    num_blocks=num_csp_blocks,
                    add_identity=False,
                )
            )
        self.out_convs = nn.ModuleList(
            [ConvModule(in_channels[i], out_channels, 1) for i in range(len(in_channels))]
        )

    def forward(self, inputs: Sequence[torch.Tensor]):
        assert len(inputs) == len(self.in_channels)
        inner_outs = [inputs[-1]]
        for idx in range(len(self.in_channels) - 1, 0, -1):
            feat_heigh = self.reduce_layers[len(self.in_channels) - 1 - idx](inner_outs[0])
            inner_outs[0] = feat_heigh
            upsample_feat = self.upsample(feat_heigh)
            feat_low = inputs[idx - 1]
            inner_out = self.top_down_blocks[len(self.in_channels) - 1 - idx](
                torch.cat([upsample_feat, feat_low], 1)
            )
            inner_outs.insert(0, inner_out)

        outs = [inner_outs[0]]
        for idx in range(len(self.in_channels) - 1):
            downsample_feat = self.downsamples[idx](outs[-1])
            out = self.bottom_up_blocks[idx](
                torch.cat([downsample_feat, inner_outs[idx + 1]], 1)
            )
            outs.append(out)

        for idx, conv in enumerate(self.out_convs):
            outs[idx] = conv(outs[idx])
        return tuple(outs)


class YOLOXHead(nn.Module):
    def __init__(
        self,
        num_classes: int = 80,
        in_channels: int = 320,
        feat_channels: int = 320,
        stacked_convs: int = 2,
        strides: Sequence[int] = (8, 16, 32),
    ):
        super().__init__()
        self.num_classes = num_classes
        self.cls_out_channels = num_classes
        self.in_channels = in_channels
        self.feat_channels = feat_channels
        self.stacked_convs = stacked_convs
        self.strides = list(strides)
        self.multi_level_cls_convs = nn.ModuleList()
        self.multi_level_reg_convs = nn.ModuleList()
        self.multi_level_conv_cls = nn.ModuleList()
        self.multi_level_conv_reg = nn.ModuleList()
        self.multi_level_conv_obj = nn.ModuleList()
        for _ in self.strides:
            self.multi_level_cls_convs.append(self._build_stacked_convs())
            self.multi_level_reg_convs.append(self._build_stacked_convs())
            self.multi_level_conv_cls.append(nn.Conv2d(feat_channels, num_classes, 1))
            self.multi_level_conv_reg.append(nn.Conv2d(feat_channels, 4, 1))
            self.multi_level_conv_obj.append(nn.Conv2d(feat_channels, 1, 1))

    def _build_stacked_convs(self) -> nn.Sequential:
        layers = []
        for i in range(self.stacked_convs):
            chn = self.in_channels if i == 0 else self.feat_channels
            layers.append(ConvModule(chn, self.feat_channels, 3, padding=1))
        return nn.Sequential(*layers)

    def forward(self, feats: Sequence[torch.Tensor]):
        cls_scores, bbox_preds, objectnesses = [], [], []
        for feat, cls_convs, reg_convs, conv_cls, conv_reg, conv_obj in zip(
            feats,
            self.multi_level_cls_convs,
            self.multi_level_reg_convs,
            self.multi_level_conv_cls,
            self.multi_level_conv_reg,
            self.multi_level_conv_obj,
        ):
            cls_feat = cls_convs(feat)
            reg_feat = reg_convs(feat)
            cls_scores.append(conv_cls(cls_feat))
            bbox_preds.append(conv_reg(reg_feat))
            objectnesses.append(conv_obj(reg_feat))
        return cls_scores, bbox_preds, objectnesses


class YOLOX(nn.Module):
    """YOLOX-X matching the HumanArt MMDet checkpoint layout."""

    def __init__(self):
        super().__init__()
        self.backbone = CSPDarknet(deepen_factor=1.33, widen_factor=1.25)
        self.neck = YOLOXPAFPN(in_channels=[320, 640, 1280], out_channels=320, num_csp_blocks=4)
        self.bbox_head = YOLOXHead(
            num_classes=80, in_channels=320, feat_channels=320, stacked_convs=2, strides=(8, 16, 32)
        )

    def forward(self, x: torch.Tensor):
        return self.bbox_head(self.neck(self.backbone(x)))


def _mlvl_priors(featmap_sizes, strides, dtype, device) -> torch.Tensor:
    """Center priors with stride, shape (N, 4) = (cx, cy, stride, stride)."""
    priors = []
    for (h, w), stride in zip(featmap_sizes, strides):
        yv, xv = torch.meshgrid(
            torch.arange(h, device=device, dtype=dtype),
            torch.arange(w, device=device, dtype=dtype),
            indexing="ij",
        )
        # MMDet MlvlPointGenerator offset=0 → centers at (x+0)*stride? 
        # Actually with_stride=True priors are (x * stride, y * stride, stride, stride)
        # For YOLOX offset=0: cx = (x + 0) * stride? Check mmdet...
        # MlvlPointGenerator: xx = (x + offset) * stride; with offset=0 → x * stride
        # But YOLOX typically uses (x + 0.5). MMDet YOLOX uses offset=0, so x * stride.
        # Looking at decode: xys = bbox_preds[..., :2] * priors[:, 2:] + priors[:, :2]
        # Official YOLOX uses grid + 0.5. MMDet with offset=0 uses grid*stride as prior center
        # and delta is relative. This matches MMDet.
        flat = torch.stack(
            [
                xv.reshape(-1) * stride,
                yv.reshape(-1) * stride,
                torch.full((h * w,), float(stride), device=device, dtype=dtype),
                torch.full((h * w,), float(stride), device=device, dtype=dtype),
            ],
            dim=-1,
        )
        priors.append(flat)
    return torch.cat(priors, dim=0)


def _bbox_decode(priors: torch.Tensor, bbox_preds: torch.Tensor) -> torch.Tensor:
    xys = bbox_preds[..., :2] * priors[:, 2:] + priors[:, :2]
    whs = bbox_preds[..., 2:].exp() * priors[:, 2:]
    tl = xys - whs / 2
    br = xys + whs / 2
    return torch.cat([tl, br], dim=-1)


@torch.no_grad()
def decode_person_boxes(
    cls_scores,
    bbox_preds,
    objectnesses,
    *,
    score_thr: float = 0.5,
    nms_thr: float = 0.65,
    person_class: int = 0,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Decode YOLOX outputs → per-image person boxes/scores in letterbox coords."""
    num_imgs = cls_scores[0].shape[0]
    featmap_sizes = [c.shape[2:] for c in cls_scores]
    strides = [8, 16, 32]
    priors = _mlvl_priors(featmap_sizes, strides, cls_scores[0].dtype, cls_scores[0].device)

    flat_cls = torch.cat(
        [c.permute(0, 2, 3, 1).reshape(num_imgs, -1, c.shape[1]) for c in cls_scores], dim=1
    ).sigmoid()
    flat_bbox = torch.cat(
        [b.permute(0, 2, 3, 1).reshape(num_imgs, -1, 4) for b in bbox_preds], dim=1
    )
    flat_obj = torch.cat(
        [o.permute(0, 2, 3, 1).reshape(num_imgs, -1) for o in objectnesses], dim=1
    ).sigmoid()
    flat_boxes = _bbox_decode(priors, flat_bbox)

    results = []
    for i in range(num_imgs):
        person_scores = flat_cls[i, :, person_class] * flat_obj[i]
        keep = person_scores >= score_thr
        boxes = flat_boxes[i][keep]
        scores = person_scores[keep]
        if boxes.numel() == 0:
            results.append(
                (np.zeros((0, 4), dtype=np.float32), np.zeros((0,), dtype=np.float32))
            )
            continue
        keep_idx = nms(boxes, scores, nms_thr)
        boxes = boxes[keep_idx].detach().float().cpu().numpy()
        scores = scores[keep_idx].detach().float().cpu().numpy()
        results.append((boxes, scores))
    return results


class YOLOXTorchDetector:
    """Batched YOLOX-X person detector (PyTorch / HumanArt checkpoint)."""

    def __init__(
        self,
        ckpt_path: str | Path | None = None,
        input_size: tuple[int, int] = (640, 640),
        score_thr: float = 0.5,
        nms_thr: float = 0.65,
        device: str = "cuda:0",
    ):
        self.input_size = input_size
        self.score_thr = score_thr
        self.nms_thr = nms_thr
        self.device = torch.device(device if torch.cuda.is_available() else "cpu")

        path = Path(ckpt_path) if ckpt_path else download_yolox_x_checkpoint()
        self.model = YOLOX().to(self.device).eval()
        ckpt = torch.load(path, map_location="cpu", weights_only=False)
        sd = ckpt["state_dict"] if isinstance(ckpt, dict) and "state_dict" in ckpt else ckpt
        missing, unexpected = self.model.load_state_dict(sd, strict=False)
        # Ignore loss_* / assigner leftovers if any.
        missing = [m for m in missing if not m.startswith("bbox_head.loss")]
        if missing:
            raise RuntimeError(f"YOLOX missing keys ({len(missing)}): {missing[:8]}…")
        if unexpected:
            # Checkpoint may contain TTA wrapper keys — ignore if core loaded.
            pass
        print(f"[YOLOXTorchDetector] Loaded {path.name} on {self.device}")

    def detect(self, img_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        boxes_list, scores_list = self.detect_batch([img_bgr])
        return boxes_list[0], scores_list[0]

    def detect_batch(
        self, imgs_bgr: Sequence[np.ndarray]
    ) -> tuple[list[np.ndarray], list[np.ndarray]]:
        if not imgs_bgr:
            return [], []
        tensors = []
        ratios = []
        for img in imgs_bgr:
            padded, ratio = self._preprocess(img)
            tensors.append(torch.from_numpy(padded.transpose(2, 0, 1)))
            ratios.append(ratio)
        batch = torch.stack(tensors, dim=0).float().to(self.device, non_blocking=True)
        with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=self.device.type == "cuda"):
            cls_scores, bbox_preds, objectnesses = self.model(batch)
        # Cast back to fp32 for stable decode/NMS.
        cls_scores = [c.float() for c in cls_scores]
        bbox_preds = [b.float() for b in bbox_preds]
        objectnesses = [o.float() for o in objectnesses]
        decoded = decode_person_boxes(
            cls_scores,
            bbox_preds,
            objectnesses,
            score_thr=self.score_thr,
            nms_thr=self.nms_thr,
        )
        boxes_out, scores_out = [], []
        for (boxes, scores), ratio in zip(decoded, ratios):
            if boxes.size:
                boxes = boxes / ratio
            boxes_out.append(boxes.astype(np.float32))
            scores_out.append(scores.astype(np.float32))
        return boxes_out, scores_out

    def _preprocess(self, img: np.ndarray) -> tuple[np.ndarray, float]:
        import cv2

        h, w = img.shape[:2]
        ratio = min(self.input_size[0] / h, self.input_size[1] / w)
        resized = cv2.resize(
            img, (int(w * ratio), int(h * ratio)), interpolation=cv2.INTER_LINEAR
        )
        padded = np.full((self.input_size[0], self.input_size[1], 3), 114, dtype=np.uint8)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        return padded, ratio
