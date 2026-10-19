"""SOMA (GEM-X) → SMPL-X display mesh for golf phase previews."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from motion_agent.mesh.average_shape import (
    average_smplx_shape_path,
    load_average_smplx_betas,
    load_average_smplx_shape,
)

LOG = logging.getLogger("motion_agent.mesh.soma_to_smplx")

GEMX_ROOT = Path(os.environ.get("CEREBEL_GEMX_ROOT", "/root/GEM-X")).resolve()
SOMA_ASSETS = Path(
    os.environ.get("CEREBEL_SOMA_ASSETS", str(GEMX_ROOT / "inputs" / "soma_assets"))
)
SMPLX_MODEL_DIR = Path(
    os.environ.get(
        "CEREBEL_SMPLX_MODEL_DIR",
        "/root/EgoGen/motion/data/smplx/models",
    )
)
CACHE_NAME = "smplx_display_mesh.npz"
MAX_CLIP_FRAMES = int(os.environ.get("MOTION_AGENT_SMPLX_MAX_CLIP_FRAMES", "90"))

# SOMA77 key → SMPL-X joint index (body joints 0..21).
_SOMA_TO_SMPLX: dict[str, int] = {
    "hips": 0,
    "l_hip": 1,
    "r_hip": 2,
    "l_knee": 4,
    "r_knee": 5,
    "l_ankle": 7,
    "r_ankle": 8,
    "l_toe": 10,
    "r_toe": 11,
    "head": 15,
    "l_shoulder": 16,
    "r_shoulder": 17,
    "l_elbow": 18,
    "r_elbow": 19,
    "l_wrist": 20,
    "r_wrist": 21,
}


@dataclass
class SmplxMeshSequence:
    faces: np.ndarray  # (F, 3) int32
    verts: np.ndarray  # (T, V, 3) float32
    fps: float
    shape_source: str
    hpe_results: str
    session_id: str


def _ensure_gemx_path() -> None:
    root = str(GEMX_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _get_body_params(pred: dict, key: str) -> dict:
    if key in pred:
        return pred[key]
    suffix = key.replace("body_params_", "")
    for k in pred:
        if k.endswith(f"_params_{suffix}"):
            return pred[k]
    raise KeyError(f"Missing `{key}` in prediction output.")


def resolve_hpe_results(session_dir: Path) -> Path | None:
    """Locate GEM-X hpe_results.pt for a session directory."""
    gemx_json = session_dir / "gemx.json"
    if gemx_json.is_file():
        try:
            meta = json.loads(gemx_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        raw = meta.get("hpe_results") or meta.get("hpe_results_path")
        if raw:
            p = Path(str(raw)).expanduser()
            if p.is_file():
                return p.resolve()
    sid = session_dir.name
    cand = GEMX_ROOT / "outputs" / "cerebel" / sid / "hpe_results.pt"
    if cand.is_file():
        return cand.resolve()
    return None


def smplx_mesh_status(session_dir: Path) -> dict[str, Any]:
    hpe = resolve_hpe_results(session_dir)
    shape = load_average_smplx_shape()
    cache = session_dir / "ik" / CACHE_NAME
    return {
        "error": False,
        "session_id": session_dir.name,
        "hpe_available": hpe is not None,
        "hpe_results": str(hpe) if hpe else None,
        "cache_ready": cache.is_file(),
        "shape_source": shape["path"],
        "num_betas": int(shape.get("num_betas") or 10),
    }


def _device() -> str:
    raw = os.environ.get("CEREBEL_GEMX_DEVICE") or os.environ.get(
        "MOTION_AGENT_SMPLX_DEVICE", "cuda:0"
    )
    try:
        import torch

        if str(raw).startswith("cuda") and not torch.cuda.is_available():
            return "cpu"
    except Exception:  # noqa: BLE001
        return "cpu"
    return str(raw)


def _as_device(params: dict, device: str) -> dict:
    import torch

    out = {}
    for k, v in params.items():
        if hasattr(v, "to"):
            out[k] = v.to(device)
        else:
            out[k] = v
    return out


def _load_soma_joints(
    hpe_results: Path,
    device: str,
    frame_slice: slice | None = None,
) -> np.ndarray:
    """Return (T, 77, 3) SOMA joints in Y-up metres (global)."""
    _ensure_gemx_path()
    import torch
    from gem.utils.soma_utils.soma_layer import SomaLayer

    from cerebel_ik_retarget import normalize_soma77_joints

    pred = torch.load(str(hpe_results), map_location="cpu", weights_only=False)
    soma = SomaLayer(
        data_root=str(SOMA_ASSETS),
        low_lod=True,
        device=device,
        identity_model_type="mhr",
        mode="warp",
    )
    params = _get_body_params(pred, "body_params_global")
    body_pose = params["body_pose"]
    T = int(body_pose.shape[0])
    if frame_slice is not None:
        start = int(frame_slice.start or 0)
        stop = int(frame_slice.stop if frame_slice.stop is not None else T)
        start = max(0, min(T, start))
        stop = max(start, min(T, stop))
    else:
        start, stop = 0, T
    chunk = int(os.environ.get("MOTION_AGENT_SOMA_JOINT_CHUNK", "128"))
    joints_list: list[np.ndarray] = []
    with torch.no_grad():
        for s in range(start, stop, chunk):
            e = min(stop, s + chunk)
            slice_params = {
                k: (
                    v[s:e]
                    if hasattr(v, "shape")
                    and len(getattr(v, "shape", ())) > 0
                    and int(v.shape[0]) == T
                    else v
                )
                for k, v in params.items()
            }
            out = soma(**_as_device(slice_params, device))
            joints_list.append(out["joints"].float().detach().cpu().numpy())
    joints = np.concatenate(joints_list, axis=0)
    return normalize_soma77_joints(joints).astype(np.float32)


def _fit_smplx_chunk(
    soma_joints: np.ndarray,
    *,
    betas: np.ndarray,
    device: str,
    steps: int,
    lr: float,
    model,
    soma_idx,
    smplx_idx,
    hips_idx: int,
) -> np.ndarray:
    """Fit one chunk; return verts (T, V, 3)."""
    import torch

    T = int(soma_joints.shape[0])
    dev = next(model.parameters()).device
    target = torch.as_tensor(soma_joints, dtype=torch.float32, device=dev)
    root_t = target[:, hips_idx, :].clone()
    target_c = target - root_t[:, None, :]

    betas_t = torch.as_tensor(betas, dtype=torch.float32, device=dev).view(1, -1)
    betas_t = betas_t.expand(T, -1).contiguous()

    global_orient = torch.zeros(T, 3, device=dev, requires_grad=True)
    body_pose = torch.zeros(T, model.NUM_BODY_JOINTS * 3, device=dev, requires_grad=True)
    transl = root_t.detach().clone().requires_grad_(True)

    opt = torch.optim.Adam([global_orient, body_pose, transl], lr=lr)

    def _forward(go, bp, tr):
        kwargs = {
            "betas": betas_t,
            "global_orient": go,
            "body_pose": bp,
            "transl": tr,
        }
        # Keep optional face/hand poses batched with T (smplx defaults are B=1).
        zeros3 = torch.zeros(T, 3, device=dev, dtype=torch.float32)
        kwargs["jaw_pose"] = zeros3
        kwargs["leye_pose"] = zeros3
        kwargs["reye_pose"] = zeros3
        if getattr(model, "num_expression_coeffs", 0):
            kwargs["expression"] = torch.zeros(
                T, int(model.num_expression_coeffs), device=dev, dtype=torch.float32
            )
        hand_dim = 15 * 3  # non-PCA axis-angle hands
        if hasattr(model, "left_hand_pose"):
            kwargs["left_hand_pose"] = torch.zeros(T, hand_dim, device=dev, dtype=torch.float32)
        if hasattr(model, "right_hand_pose"):
            kwargs["right_hand_pose"] = torch.zeros(T, hand_dim, device=dev, dtype=torch.float32)
        return model(**kwargs)

    for _ in range(int(steps)):
        opt.zero_grad(set_to_none=True)
        out = _forward(global_orient, body_pose, transl)
        pred_j = out.joints[:, :22, :]
        pred_root = pred_j[:, 0, :]
        pred_c = pred_j - pred_root[:, None, :]
        loss = torch.mean((pred_c[:, smplx_idx] - target_c[:, soma_idx]) ** 2)
        loss = loss + 1e-3 * torch.mean(body_pose**2) + 1e-4 * torch.mean(global_orient**2)
        loss.backward()
        opt.step()

    with torch.no_grad():
        out = _forward(global_orient, body_pose, transl)
        pred_j = out.joints[:, :22, :]
        transl_final = transl + (root_t - pred_j[:, 0, :])
        out = _forward(global_orient, body_pose, transl_final)
        return out.vertices.detach().cpu().numpy().astype(np.float32)


def _fit_smplx_to_soma_joints(
    soma_joints: np.ndarray,
    *,
    betas: np.ndarray,
    device: str,
    steps: int = 60,
    lr: float = 0.08,
) -> tuple[np.ndarray, np.ndarray]:
    """Fit SMPL-X pose in chunks; return faces, verts (T,V,3)."""
    import torch
    import smplx

    from cerebel_ik_retarget import SOMA_JOINT

    T = int(soma_joints.shape[0])
    dev = torch.device(device if not device.startswith("cuda") else device)
    chunk = int(os.environ.get("MOTION_AGENT_SMPLX_FIT_CHUNK", "64"))
    # Fewer steps on long sequences keeps first-build latency reasonable.
    if T > 400:
        steps = min(steps, 40)

    model = smplx.create(
        str(SMPLX_MODEL_DIR),
        model_type="smplx",
        gender="neutral",
        num_betas=int(betas.shape[0]),
        use_pca=False,
        flat_hand_mean=True,
        batch_size=min(chunk, max(T, 1)),
    ).to(dev)
    model.eval()
    for p in model.parameters():
        p.requires_grad_(False)

    pairs: list[tuple[int, int]] = []
    for soma_key, smplx_j in _SOMA_TO_SMPLX.items():
        pairs.append((int(SOMA_JOINT[soma_key]), int(smplx_j)))
    soma_idx = torch.tensor([a for a, _ in pairs], dtype=torch.long, device=dev)
    smplx_idx = torch.tensor([b for _, b in pairs], dtype=torch.long, device=dev)
    hips_idx = int(SOMA_JOINT["hips"])

    verts_list: list[np.ndarray] = []
    for start in range(0, T, chunk):
        end = min(T, start + chunk)
        LOG.info("SMPL-X fit frames %s:%s / %s", start, end, T)
        # Recreate model when chunk length differs from batch_size buffers.
        n = end - start
        if int(getattr(model, "batch_size", n)) != n:
            model = smplx.create(
                str(SMPLX_MODEL_DIR),
                model_type="smplx",
                gender="neutral",
                num_betas=int(betas.shape[0]),
                use_pca=False,
                flat_hand_mean=True,
                batch_size=n,
            ).to(dev)
            model.eval()
            for p in model.parameters():
                p.requires_grad_(False)
        verts_list.append(
            _fit_smplx_chunk(
                soma_joints[start:end],
                betas=betas,
                device=device,
                steps=steps,
                lr=lr,
                model=model,
                soma_idx=soma_idx,
                smplx_idx=smplx_idx,
                hips_idx=hips_idx,
            )
        )
    verts = np.concatenate(verts_list, axis=0)
    faces = np.asarray(model.faces, dtype=np.int32)
    return faces, verts


def _cache_path(session_dir: Path) -> Path:
    return session_dir / "ik" / CACHE_NAME


def _cache_is_valid(session_dir: Path, hpe: Path) -> bool:
    cache = _cache_path(session_dir)
    if not cache.is_file():
        return False
    try:
        data = np.load(cache, allow_pickle=False)
        meta_hpe = str(data["hpe_results"][0]) if "hpe_results" in data.files else ""
        meta_mtime = int(data["shape_mtime_ns"][0]) if "shape_mtime_ns" in data.files else -1
        shape = load_average_smplx_shape()
        return meta_hpe == str(hpe) and meta_mtime == int(shape["mtime_ns"])
    except Exception:  # noqa: BLE001
        return False


def build_smplx_mesh_sequence(
    session_dir: Path,
    *,
    fps: float | None = None,
    force: bool = False,
    fit_steps: int = 50,
    frame_slice: slice | None = None,
) -> SmplxMeshSequence:
    """Build or load cached SMPL-X display mesh for a session.

    If ``frame_slice`` is set and no full cache exists, only that frame range is
    fitted (fast path for golf phase clips).
    """
    session_dir = session_dir.expanduser().resolve()
    hpe = resolve_hpe_results(session_dir)
    if hpe is None:
        raise FileNotFoundError(f"No GEM-X hpe_results.pt for session {session_dir.name}")

    shape = load_average_smplx_shape()
    betas = load_average_smplx_betas(num_betas=int(shape.get("num_betas") or 10))
    cache = _cache_path(session_dir)

    if frame_slice is None and (not force) and _cache_is_valid(session_dir, hpe):
        data = np.load(cache, allow_pickle=False)
        return SmplxMeshSequence(
            faces=np.asarray(data["faces"], dtype=np.int32),
            verts=np.asarray(data["verts"], dtype=np.float32),
            fps=float(data["fps"][0]) if "fps" in data.files else float(fps or 30.0),
            shape_source=str(shape["path"]),
            hpe_results=str(hpe),
            session_id=session_dir.name,
        )

    # Resolve fps from session artifacts when possible.
    if fps is None:
        fps = 30.0
        for name in ("gemx.json", "summary.json", "header.json"):
            p = session_dir / name
            if not p.is_file():
                continue
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for key in ("fps", "FPS", "nominalFps"):
                if key in meta and meta[key]:
                    fps = float(meta[key])
                    break
            if "metadata" in meta and isinstance(meta["metadata"], dict):
                m = meta["metadata"]
                if m.get("fps"):
                    fps = float(m["fps"])
                elif m.get("nominalFps"):
                    fps = float(m["nominalFps"])

    device = _device()
    LOG.info(
        "building SMPL-X display mesh session=%s device=%s hpe=%s slice=%s",
        session_dir.name,
        device,
        hpe,
        frame_slice,
    )
    joints = _load_soma_joints(hpe, device=device, frame_slice=frame_slice)
    faces, verts = _fit_smplx_to_soma_joints(
        joints, betas=betas, device=device, steps=fit_steps
    )

    # Only persist full-session caches.
    if frame_slice is None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            cache,
            faces=faces,
            verts=verts,
            fps=np.asarray([float(fps)], dtype=np.float32),
            shape_mtime_ns=np.asarray([int(shape["mtime_ns"])], dtype=np.int64),
            hpe_results=np.asarray([str(hpe)]),
            shape_source=np.asarray([str(shape["path"])]),
        )
    return SmplxMeshSequence(
        faces=faces,
        verts=verts,
        fps=float(fps),
        shape_source=str(shape["path"]),
        hpe_results=str(hpe),
        session_id=session_dir.name,
    )


def get_smplx_mesh_clip(
    session_dir: Path,
    t0: float,
    t1: float,
    *,
    fps: float | None = None,
    max_frames: int = MAX_CLIP_FRAMES,
) -> dict[str, Any]:
    """Return a JSON-serializable SMPL-X mesh clip for [t0, t1]."""
    session_dir = session_dir.expanduser().resolve()
    hpe = resolve_hpe_results(session_dir)
    if hpe is None:
        return {
            "error": True,
            "fallback": "raw_video",
            "message": f"No GEM-X hpe_results.pt for session {session_dir.name}",
            "view": "smplx",
        }

    t0 = float(t0)
    t1 = float(t1)
    if not np.isfinite(t0) or not np.isfinite(t1) or t1 <= t0:
        return {
            "error": True,
            "fallback": "raw_video",
            "message": "invalid time window",
            "view": "smplx",
        }

    # Probe fps / length without fitting when cache exists.
    try:
        if _cache_is_valid(session_dir, hpe):
            seq = build_smplx_mesh_sequence(session_dir, fps=fps)
            n = int(seq.verts.shape[0])
            i0 = int(max(0, min(n - 1, round(t0 * seq.fps))))
            i1 = int(max(i0 + 1, min(n, round(t1 * seq.fps))))
            verts = seq.verts[i0:i1]
            faces = seq.faces
            used_fps = float(seq.fps)
            shape_source = seq.shape_source
            hpe_s = seq.hpe_results
        else:
            # Fast path: fit only the phase window (+ small pad).
            probe_fps = float(fps or 30.0)
            for name in ("gemx.json", "summary.json"):
                p = session_dir / name
                if p.is_file():
                    try:
                        meta = json.loads(p.read_text(encoding="utf-8"))
                        if meta.get("fps"):
                            probe_fps = float(meta["fps"])
                    except (OSError, json.JSONDecodeError, TypeError, ValueError):
                        pass
            # Estimate total frames from hpe without SomaLayer.
            import torch

            pred = torch.load(str(hpe), map_location="cpu", weights_only=False)
            params = _get_body_params(pred, "body_params_global")
            n_total = int(params["body_pose"].shape[0])
            i0 = int(max(0, min(n_total - 1, round(t0 * probe_fps))))
            i1 = int(max(i0 + 1, min(n_total, round(t1 * probe_fps))))
            pad = 2
            s0 = max(0, i0 - pad)
            s1 = min(n_total, i1 + pad)
            seq = build_smplx_mesh_sequence(
                session_dir,
                fps=probe_fps,
                frame_slice=slice(s0, s1),
                fit_steps=45,
            )
            local_i0 = i0 - s0
            local_i1 = max(local_i0 + 1, i1 - s0)
            verts = seq.verts[local_i0:local_i1]
            faces = seq.faces
            used_fps = float(seq.fps)
            shape_source = seq.shape_source
            hpe_s = seq.hpe_results
    except Exception as exc:  # noqa: BLE001
        LOG.exception("smplx mesh build failed for %s", session_dir)
        return {
            "error": True,
            "fallback": "raw_video",
            "message": f"{type(exc).__name__}: {exc}",
            "view": "smplx",
        }

    if verts.shape[0] == 0:
        return {
            "error": True,
            "fallback": "raw_video",
            "message": "empty mesh clip",
            "view": "smplx",
        }

    step = 1
    if verts.shape[0] > int(max_frames):
        step = int(np.ceil(verts.shape[0] / float(max_frames)))
        verts = verts[::step]

    # Ground / center for stable orbit camera.
    v = verts.copy()
    center = v.reshape(-1, 3).mean(axis=0)
    v -= center.reshape(1, 1, 3)
    ymin = float(v[..., 1].min())
    v[..., 1] -= ymin

    frame_times = [round(t0 + k * step / float(used_fps), 4) for k in range(v.shape[0])]
    # Packed binary-friendly payload (nested JSON is ~10MB+ and breaks some browsers).
    import base64

    verts_f32 = np.ascontiguousarray(v, dtype=np.float32).reshape(-1)
    faces_i32 = np.ascontiguousarray(faces, dtype=np.int32).reshape(-1)
    return {
        "error": False,
        "view": "smplx",
        "session_id": session_dir.name,
        "start_time": t0,
        "end_time": t1,
        "fps": float(used_fps) / float(step),
        "source_fps": float(used_fps),
        "frame_count": int(v.shape[0]),
        "frame_times": frame_times,
        "vertex_count": int(v.shape[1]),
        "encoding": "f32_i32_b64",
        "faces_b64": base64.b64encode(faces_i32.tobytes()).decode("ascii"),
        "verts_b64": base64.b64encode(verts_f32.tobytes()).decode("ascii"),
        "shape_source": shape_source,
        "shape_path": str(average_smplx_shape_path()),
        "hpe_results": hpe_s,
        "note": (
            "SMPL-X display mesh retargeted from GEM-X SOMA joints; "
            "shape betas from server average_smplx_shape.json (not GEM-X identity)."
        ),
    }
