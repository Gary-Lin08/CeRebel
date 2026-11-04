#!/usr/bin/env python3
"""Submit finished CeRebel sessions to the warm GEM-X worker (models stay loaded)."""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

LOG = logging.getLogger("cerebel.gemx")

DEFAULT_GEMX_ROOT = Path(os.environ.get("CEREBEL_GEMX_ROOT", "/root/GEM-X"))
GEMX_PYTHON = Path(os.environ.get("CEREBEL_GEMX_PYTHON", str(DEFAULT_GEMX_ROOT / ".venv" / "bin" / "python")))
DEFAULT_OUTPUT_ROOT = Path(
    os.environ.get("CEREBEL_GEMX_OUTPUT_ROOT", str(DEFAULT_GEMX_ROOT / "outputs" / "cerebel"))
)
IK_ENABLED = os.environ.get("CEREBEL_IK_ENABLED", "1").lower() not in ("0", "false", "no")
VISER_PORT = int(os.environ.get("CEREBEL_VISER_PORT", "8090"))
KINETIC_LENS_PORT = int(os.environ.get("CEREBEL_KINETIC_LENS_PORT", "8091"))
try:
    from cerebel_config import fake_egocentric_enabled as _fake_egocentric_enabled
except ImportError:  # pragma: no cover
    def _fake_egocentric_enabled() -> bool:
        return os.environ.get("CEREBEL_FAKE_EGOCENTRIC", "0").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
MIN_FRAMES = int(os.environ.get("CEREBEL_GEMX_MIN_FRAMES", "8"))
# "local" = the warm worker on this machine; "modal" = deploy/modal_gemx.py on Modal GPUs.
GPU_BACKEND = os.environ.get("CEREBEL_GPU_BACKEND", "local").strip().lower()
WORKER_HOST = os.environ.get("CEREBEL_GEMX_WORKER_HOST", "127.0.0.1")
WORKER_PORT = int(os.environ.get("CEREBEL_GEMX_WORKER_PORT", "61017"))
WORKER_TIMEOUT_S = float(os.environ.get("CEREBEL_GEMX_WORKER_TIMEOUT_S", "3600"))
WORKER_WAIT_READY_S = float(os.environ.get("CEREBEL_GEMX_WORKER_WAIT_READY_S", "600"))
# A job whose connection is refused waits this long for the worker to come back (a supervisor
# restart reloads the models in ~2 min) before it fails as "gpu_unavailable".
WORKER_SUBMIT_WAIT_S = float(os.environ.get("CEREBEL_GEMX_WORKER_SUBMIT_WAIT_S", "300"))
_WAIT_GAVE_UP_AT = float("-inf")  # time.monotonic() when a wait for the worker last ran out
# CEREBEL_GPU_BACKEND=modal: the deployed app (deploy/modal_gemx.py) and how long one call may take.
# The Modal function itself stops at 20 min; this client-side limit frees the queue slot if the
# connection to Modal hangs, and cancels the call so it stops billing.
MODAL_APP = os.environ.get("CEREBEL_MODAL_APP") or "cerebel-gemx"
MODAL_CALL_TIMEOUT_S = float(os.environ.get("CEREBEL_MODAL_CALL_TIMEOUT_S") or "1500")
# /api/health and the upload check look the class up on Modal (no GPU starts); the answer is
# cached this long when it worked, and 30 s when it did not.
MODAL_STATUS_TTL_S = float(os.environ.get("CEREBEL_MODAL_STATUS_TTL_S") or "300")
MODAL_STATUS_TIMEOUT_S = float(os.environ.get("CEREBEL_MODAL_STATUS_TIMEOUT_S") or "10")
_MODAL_DOWN_TTL_S = 30.0
_MODAL_STATUS: dict = {"state": None, "at": float("-inf"), "error": None}
_MODAL_STATUS_LOCK = threading.Lock()
# IK, the display export and the privacy strip run on this machine's CPU after the GPU step.
# With Modal, CEREBEL_QUEUE_WORKERS jobs can reach that step together; this caps how many do.
POSTPROCESS_WORKERS = max(1, int(os.environ.get("CEREBEL_POSTPROCESS_WORKERS") or "4"))
_POSTPROCESS_SLOTS = threading.BoundedSemaphore(POSTPROCESS_WORKERS)


@dataclass
class PipelineResult:
    session_id: str
    video_path: Path
    output_dir: Path
    hpe_results: Path
    fps: float
    ok: bool
    error: str | None = None
    opensim_motion: Path | None = None
    ik_ok: bool = False
    ik_error: str | None = None
    # Where it failed: "input", "gpu_unavailable", "gemx" or "ik" (None when it worked).
    failed_at: str | None = None


class WorkerUnavailable(RuntimeError):
    """The warm GEM-X worker could not be reached, so nothing ran on the GPU."""


def _read_header(session_dir: Path) -> dict:
    path = session_dir / "header.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _session_fps(header: dict, fallback: float = 30.0) -> float:
    fps = header.get("target_fps") or fallback
    try:
        fps = float(fps)
    except (TypeError, ValueError):
        return fallback
    return fps if fps > 0 else fallback


def _jpeg_dir(session_dir: Path) -> Path:
    jpeg_dir = session_dir / "jpegs"
    if not jpeg_dir.is_dir():
        raise FileNotFoundError(f"no jpegs/ in {session_dir} (need PIXEL_FORMAT_JPEG captures)")
    n = sum(1 for _ in jpeg_dir.glob("*.jpg"))
    if n < MIN_FRAMES:
        raise RuntimeError(f"need ≥{MIN_FRAMES} JPEG frames, found {n}")
    return jpeg_dir


def _find_source_video(session_dir: Path) -> Path | None:
    upload = session_dir / "upload"
    if not upload.is_dir():
        return None
    candidates = sorted(upload.glob("source_video.*"))
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def _jpeg_count(session_dir: Path) -> int:
    jpeg_dir = session_dir / "jpegs"
    if not jpeg_dir.is_dir():
        return 0
    return sum(1 for _ in jpeg_dir.glob("*.jpg"))


def _resolve_gemx_input(session_dir: Path) -> tuple[Path | None, Path | None]:
    """Prefer ready jpegs; else web-upload source video for overlapped extract.

    Returns (jpeg_dir, video_path) — exactly one should be set for a valid input.
    """
    n = _jpeg_count(session_dir)
    if n >= MIN_FRAMES:
        return session_dir / "jpegs", None
    video = _find_source_video(session_dir)
    if video is not None:
        LOG.info(
            "session %s: jpegs not ready (%s frames); using source video %s",
            session_dir.name,
            n,
            video.name,
        )
        return None, video
    if n > 0:
        raise RuntimeError(f"need ≥{MIN_FRAMES} JPEG frames, found {n}")
    raise FileNotFoundError(
        f"no jpegs/ or upload/source_video.* in {session_dir}"
    )


def _rpc(req: dict, *, timeout: float = WORKER_TIMEOUT_S) -> dict:
    raw = (json.dumps(req) + "\n").encode("utf-8")
    with socket.create_connection((WORKER_HOST, WORKER_PORT), timeout=min(30.0, timeout)) as sock:
        sock.settimeout(timeout)
        sock.sendall(raw)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    if not buf:
        raise RuntimeError("warm worker closed connection with no reply")
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))


def wait_for_worker(timeout_s: float = WORKER_WAIT_READY_S) -> None:
    """Block until the warm worker has finished loading models."""
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        try:
            resp = _rpc({"cmd": "ping"}, timeout=5.0)
            if resp.get("ok") and resp.get("ready"):
                LOG.info("warm GEM-X worker ready at %s:%s", WORKER_HOST, WORKER_PORT)
                return
            LOG.info("warm worker still loading models…")
        except OSError as exc:
            last_err = exc
            LOG.info("waiting for warm worker %s:%s (%s)", WORKER_HOST, WORKER_PORT, exc)
        time.sleep(2.0)
    global _WAIT_GAVE_UP_AT
    _WAIT_GAVE_UP_AT = time.monotonic()
    raise TimeoutError(
        f"warm worker not ready after {timeout_s}s at {WORKER_HOST}:{WORKER_PORT}: {last_err}"
    )


def _modal_credentials() -> bool:
    """A Modal token in the environment (MODAL_TOKEN_ID/SECRET) or in ~/.modal.toml."""
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return True
    config = os.environ.get("MODAL_CONFIG_PATH") or str(Path.home() / ".modal.toml")
    return Path(config).is_file()


def _modal_lookup() -> None:
    """Resolve the deployed GEM-X class on Modal. Needs the API, starts no container."""
    import modal

    modal.Cls.from_name(MODAL_APP, "Gemx").hydrate()


def _set_modal_status(state: str, error: str | None = None) -> None:
    with _MODAL_STATUS_LOCK:
        if state != _MODAL_STATUS["state"]:
            (LOG.info if state == "remote" else LOG.warning)(
                "Modal GPU backend %s%s", state, f": {error}" if error else ""
            )
        _MODAL_STATUS.update(state=state, at=time.monotonic(), error=error)


def modal_status() -> str:
    """"remote" when the deployed Modal class can be reached, else "down" (no token, modal not
    installed, app not deployed, Modal unreachable or too slow). Cached, see MODAL_STATUS_TTL_S."""
    with _MODAL_STATUS_LOCK:
        state, at = _MODAL_STATUS["state"], _MODAL_STATUS["at"]
        ttl = MODAL_STATUS_TTL_S if state == "remote" else _MODAL_DOWN_TTL_S
        if state is not None and time.monotonic() - at < ttl:
            return state
    if not _modal_credentials():
        _set_modal_status("down", "no Modal token (MODAL_TOKEN_ID/MODAL_TOKEN_SECRET)")
        return "down"
    found: dict = {}

    def probe() -> None:
        try:
            _modal_lookup()
            found["state"] = "remote"
        except BaseException as exc:  # noqa: BLE001 - ImportError, auth, not found, network
            found["state"] = "down"
            found["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=probe, name="modal-status", daemon=True)
    thread.start()
    thread.join(MODAL_STATUS_TIMEOUT_S)
    if "state" not in found:
        found = {"state": "down", "error": f"Modal lookup took over {MODAL_STATUS_TIMEOUT_S:.0f}s"}
    _set_modal_status(found["state"], found.get("error"))
    return found["state"]


def worker_status(timeout: float = 2.0) -> str:
    """GPU state: "remote" (Modal reachable), "ready", "loading" (models still loading) or "down"."""
    if GPU_BACKEND == "modal":
        return modal_status()
    try:
        resp = _rpc({"cmd": "ping"}, timeout=timeout)
    except (OSError, ValueError, RuntimeError):
        return "down"
    if not resp.get("ok"):
        return "down"
    return "ready" if resp.get("ready") else "loading"


def run_gemx_warm(
    session_id: str,
    fps: float,
    *,
    jpeg_dir: Path | None = None,
    video_path: Path | None = None,
) -> Path:
    """Send a job to the resident warm worker (no model reload, no 2D KP video)."""
    if jpeg_dir is None and video_path is None:
        raise ValueError("run_gemx_warm requires jpeg_dir or video_path")
    DEFAULT_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    LOG.info(
        "submitting warm GEM-X job session=%s jpeg_dir=%s video_path=%s fps=%s",
        session_id,
        jpeg_dir,
        video_path,
        fps,
    )
    req: dict = {
        "cmd": "run",
        "session_id": session_id,
        "output_root": str(DEFAULT_OUTPUT_ROOT),
        "fps": fps,
        "static_cam": True,
    }
    if jpeg_dir is not None:
        req["jpeg_dir"] = str(jpeg_dir)
    if video_path is not None:
        req["video_path"] = str(video_path)
    try:
        resp = _rpc(req, timeout=WORKER_TIMEOUT_S)
    except ConnectionRefusedError as exc:
        # Refused at connect, so the job never started: safe to wait and submit once more.
        # Right after a wait ran out, fail at once instead of making every queued job wait again.
        if time.monotonic() - _WAIT_GAVE_UP_AT < WORKER_SUBMIT_WAIT_S:
            raise WorkerUnavailable(f"warm worker still down at {WORKER_HOST}:{WORKER_PORT}: {exc}") from exc
        LOG.warning(
            "warm worker refused session=%s; waiting up to %.0fs for it to come back",
            session_id,
            WORKER_SUBMIT_WAIT_S,
        )
        try:
            wait_for_worker(WORKER_SUBMIT_WAIT_S)
        except TimeoutError as exc:
            raise WorkerUnavailable(str(exc)) from exc
        resp = _rpc(req, timeout=WORKER_TIMEOUT_S)
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error") or f"warm worker failed: {resp}")
    out_dir = Path(resp["output_dir"])
    hpe = Path(resp["hpe_results"])
    if not hpe.is_file():
        raise FileNotFoundError(f"warm worker claimed success but missing {hpe}")
    LOG.info(
        "warm GEM-X done session=%s elapsed=%.1fs frames=%s → %s",
        session_id,
        float(resp.get("elapsed_s") or 0.0),
        resp.get("frames"),
        hpe,
    )
    return out_dir


def _safe_extract(archive: bytes, dest: Path) -> None:
    import io
    import tarfile

    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            target = (root / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"unsafe path in GPU result archive: {member.name}")
            if member.issym() or member.islnk():
                raise RuntimeError(f"links are not allowed in GPU result archive: {member.name}")
        tar.extractall(root)


def _is_timeout(exc: BaseException) -> bool:
    # FunctionCall.get(timeout=...) raises a TimeoutError; Modal's own classes are named *Timeout*.
    return isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__


def run_gemx_modal(session_id: str, fps: float, *, video_path: Path) -> Path:
    """Run GEM-X on Modal's autoscaling GPUs (deploy/modal_gemx.py) and unpack the result locally."""
    video = Path(video_path).read_bytes()
    try:
        import modal  # only needed when CEREBEL_GPU_BACKEND=modal

        cls = modal.Cls.from_name(MODAL_APP, "Gemx")
        call = cls().run.spawn(session_id, video, Path(video_path).suffix, float(fps))
    except Exception as exc:  # noqa: BLE001 - the job never started on a GPU
        _set_modal_status("down", f"{type(exc).__name__}: {exc}")
        raise WorkerUnavailable(f"Modal GEM-X not reachable: {type(exc).__name__}: {exc}") from exc
    LOG.info("Modal GEM-X call session=%s id=%s", session_id, getattr(call, "object_id", "?"))
    try:
        resp = call.get(timeout=MODAL_CALL_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        if not _is_timeout(exc):
            raise
        try:
            call.cancel()
        except Exception:  # noqa: BLE001
            LOG.warning("could not cancel Modal call for session %s", session_id)
        raise RuntimeError(f"Modal GEM-X job timed out after {MODAL_CALL_TIMEOUT_S:.0f}s") from exc
    _set_modal_status("remote")
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error") or "Modal GEM-X job failed")
    out_dir = DEFAULT_OUTPUT_ROOT / session_id
    _safe_extract(resp["archive"], out_dir)
    hpe = out_dir / resp["hpe_rel"]
    if not hpe.is_file():
        raise FileNotFoundError(f"Modal result missing {hpe}")
    LOG.info(
        "warm GEM-X done session=%s elapsed=%.1fs frames=%s → %s (modal)",
        session_id, float(resp.get("elapsed_s") or 0.0), resp.get("frames"), hpe,
    )
    return out_dir


def write_latest_pointer(
    data_dir: Path,
    *,
    session_id: str,
    output_dir: Path,
    hpe_results: Path,
    fps: float,
    status: str = "ready",
    error: str | None = None,
    opensim_motion: Path | None = None,
    ik_status: str | None = None,
    failed_at: str | None = None,
    session_dir: Path | None = None,
) -> Path:
    """Atomic write of the pointer the mesh visualizer watches.

    latest_mesh.json holds only the newest job; with several jobs at once (Modal) each would
    overwrite the others, so with ``session_dir`` the same payload also goes to that session's
    own SESSION_STATUS file, which the upload progress reads first (see read_session_pointer)."""
    payload = {
        "session_id": session_id,
        "status": status,
        "fps": fps,
        "output_dir": str(output_dir) if output_dir else "",
        "hpe_results": str(hpe_results) if hpe_results else "",
        "opensim_motion": str(opensim_motion) if opensim_motion else "",
        "ik_status": ik_status or "",
        "updated_at": time.time(),
        "error": error,
        "failed_at": failed_at,
        "viser_port": VISER_PORT,
        "kinetic_lens_port": KINETIC_LENS_PORT,
    }
    target = data_dir / "latest_mesh.json"
    # One temp file per writer: with parallel jobs a shared name made one job's replace() fail
    # with FileNotFoundError after another job had already moved the file.
    tmp = data_dir / f"latest_mesh.json.{os.getpid()}.{threading.get_ident()}.tmp"
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    if session_dir is not None and Path(session_dir).is_dir():
        own = Path(session_dir) / SESSION_STATUS
        own_tmp = own.with_name(own.name + ".tmp")
        own_tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        own_tmp.replace(own)
    return target


SESSION_STATUS = "gemx_status.json"


def read_session_pointer(data_dir: Path, session_id: str) -> dict | None:
    """This session's pipeline state: its own status file, else latest_mesh.json if it is about
    this session (older runs and other writers only update latest_mesh.json)."""
    for path in (Path(data_dir) / "sessions" / session_id / SESSION_STATUS, Path(data_dir) / "latest_mesh.json"):
        if not path.is_file():
            continue
        try:
            ptr = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if ptr.get("session_id") == session_id:
            return ptr
    return None


def _run_ik_subprocess(session_dir: Path, hpe_results: Path, fps: float) -> tuple[bool, Path | None, str | None]:
    """Run IK in the GEM-X venv (numpy/torch); server .venv stays minimal."""
    session_dir = session_dir.resolve()
    hpe_results = hpe_results.resolve()
    ik_script = Path(__file__).resolve().parent / "ik_pipeline.py"
    if not GEMX_PYTHON.is_file():
        return False, None, f"GEM-X python not found: {GEMX_PYTHON}"
    cmd = [
        str(GEMX_PYTHON),
        str(ik_script),
        "--session-dir",
        str(session_dir),
        "--hpe-results",
        str(hpe_results),
        "--fps",
        str(fps),
    ]
    LOG.info("starting IK subprocess session=%s", session_dir.name)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        return False, None, "IK subprocess timed out"
    except OSError as exc:
        return False, None, str(exc)

    opensim_motion = session_dir / "ik" / "opensim-motion.json"
    if proc.returncode == 0 and opensim_motion.is_file():
        if proc.stdout.strip():
            LOG.info("IK subprocess stdout:\n%s", proc.stdout.strip()[-2000:])
        return True, opensim_motion, None

    detail = (proc.stderr or proc.stdout or "").strip()
    if len(detail) > 2000:
        detail = detail[-2000:]
    LOG.error("IK subprocess failed rc=%s session=%s\n%s", proc.returncode, session_dir.name, detail)
    return False, None, detail or f"IK exited with code {proc.returncode}"


def _export_soma_display(session_dir: Path, hpe_results: Path, fps: float) -> Path | None:
    """Export SOMA mesh pack used by fake-egocentric UIs (GEM-X python)."""
    if not GEMX_PYTHON.is_file():
        LOG.warning("skip SOMA display export: GEM-X python missing")
        return None
    script = Path(__file__).resolve().parent / "soma_display_export.py"
    out_dir = session_dir / "display"
    # Always root the global mesh for fake-ego (ignore stale CEREBEL_SOMA_DISPLAY_SPACE=incam).
    space = os.environ.get("CEREBEL_SOMA_DISPLAY_SPACE", "global").strip().lower()
    if space not in ("global", "incam"):
        space = "global"
    if _fake_egocentric_enabled():
        space = "global"
    cmd = [
        str(GEMX_PYTHON),
        str(script),
        "--hpe-results",
        str(hpe_results.resolve()),
        "--out-dir",
        str(out_dir),
        "--fps",
        str(fps),
        "--space",
        space,
    ]
    LOG.info("exporting SOMA display mesh session=%s space=%s", session_dir.name, space)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as exc:
        LOG.error("SOMA display export failed: %s", exc)
        return None
    bin_path = out_dir / "soma-mesh.bin"
    if proc.returncode == 0 and bin_path.is_file():
        if proc.stdout.strip():
            LOG.info("SOMA display export: %s", proc.stdout.strip()[-500:])
        return bin_path
    detail = (proc.stderr or proc.stdout or "").strip()[-1500:]
    LOG.error("SOMA display export failed rc=%s\n%s", proc.returncode, detail)
    return None


def _sanitize_hpe(hpe_results: Path) -> None:
    """Drop head shape and unused network outputs from hpe_results.pt (see hpe_privacy.py).
    Runs after IK and the display export, so their results use the full prediction."""
    if not GEMX_PYTHON.is_file():
        LOG.error("hpe_results.pt NOT sanitized (GEM-X python missing): %s", hpe_results)
        return
    script = Path(__file__).resolve().parent / "hpe_privacy.py"
    try:
        proc = subprocess.run(
            [str(GEMX_PYTHON), str(script), str(hpe_results.resolve())],
            capture_output=True, text=True, timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        LOG.error("hpe_results.pt NOT sanitized: %s (%s)", hpe_results, exc)
        return
    if proc.returncode != 0:
        LOG.error("hpe_results.pt NOT sanitized rc=%s: %s\n%s", proc.returncode, hpe_results,
                  (proc.stderr or proc.stdout or "").strip()[-1500:])


def process_session(session_dir: Path, data_dir: Path) -> PipelineResult:
    session_id = session_dir.name
    header = _read_header(session_dir)
    fps = _session_fps(header)
    write_latest_pointer(
        data_dir,
        session_dir=session_dir,
        session_id=session_id,
        output_dir=session_dir,
        hpe_results=Path(),
        fps=fps,
        status="running",
    )
    failed_at = "input"
    try:
        jpeg_dir, video_path = _resolve_gemx_input(session_dir)
        failed_at = "gemx"
        if GPU_BACKEND == "modal":
            src = video_path or next(iter(sorted((session_dir / "upload").glob("source_video.*"))), None)
            if src is None:
                raise FileNotFoundError("Modal backend needs the uploaded video file")
            out_dir = run_gemx_modal(session_id, fps, video_path=Path(src))
        else:
            out_dir = run_gemx_warm(
                session_id, fps, jpeg_dir=jpeg_dir, video_path=video_path
            )
        candidates = [
            out_dir / "hpe_results.pt",
            out_dir / "preprocess" / "hpe_results.pt",
        ]
        hpe = next((p for p in candidates if p.is_file()), None)
        if hpe is None:
            raise FileNotFoundError(f"missing hpe_results.pt under {out_dir}")

        opensim_motion: Path | None = None
        ik_ok = False
        ik_error: str | None = None
        soma_display: Path | None = None
        with _POSTPROCESS_SLOTS:  # CPU work on this machine; see CEREBEL_POSTPROCESS_WORKERS
            if IK_ENABLED:
                try:
                    write_latest_pointer(
                        data_dir,
                        session_dir=session_dir,
                        session_id=session_id,
                        output_dir=out_dir,
                        hpe_results=hpe,
                        fps=fps,
                        status="running",
                        ik_status="running",
                    )
                    ik_ok, opensim_motion, ik_error = _run_ik_subprocess(session_dir, hpe, fps)
                except Exception as exc:
                    LOG.exception("IK pipeline failed for session %s", session_id)
                    ik_error = str(exc)

            if _fake_egocentric_enabled():
                try:
                    soma_display = _export_soma_display(session_dir, hpe, fps)
                except Exception:
                    LOG.exception("SOMA display export raised for session %s", session_id)

            _sanitize_hpe(hpe)

        write_latest_pointer(
            data_dir,
            session_dir=session_dir,
            session_id=session_id,
            output_dir=out_dir,
            hpe_results=hpe,
            fps=fps,
            status="ready",
            opensim_motion=opensim_motion,
            ik_status="ready" if ik_ok else ("error" if ik_error else "skipped"),
        )
        gemx_manifest = {
            "session_id": session_id,
            "jpeg_dir": str(jpeg_dir) if jpeg_dir else "",
            "video_path": str(video_path) if video_path else "",
            "output_dir": str(out_dir),
            "hpe_results": str(hpe),
            "fps": fps,
            # Modal is always sent the uploaded video, even when the JPEGs were ready first.
            "mode": "modal" if GPU_BACKEND == "modal" else "warm",
            "input": "jpegs" if jpeg_dir is not None and GPU_BACKEND != "modal" else "video",
            "fake_egocentric": _fake_egocentric_enabled(),
        }
        if soma_display:
            gemx_manifest["soma_display"] = str(soma_display)
        if opensim_motion:
            gemx_manifest["opensim_motion"] = str(opensim_motion)
            gemx_manifest["ik_ok"] = ik_ok
        if ik_error:
            gemx_manifest["ik_error"] = ik_error
        (session_dir / "gemx.json").write_text(
            json.dumps(gemx_manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        LOG.info(
            "GEM-X ready session=%s output=%s ik=%s",
            session_id,
            out_dir,
            "ok" if ik_ok else (ik_error or "skipped"),
        )
        return PipelineResult(
            session_id=session_id,
            video_path=jpeg_dir or video_path or (session_dir / "jpegs"),
            output_dir=out_dir,
            hpe_results=hpe,
            fps=fps,
            ok=True,
            opensim_motion=opensim_motion,
            ik_ok=ik_ok,
            ik_error=ik_error,
            failed_at="ik" if ik_error else None,
        )
    except Exception as exc:
        LOG.exception("GEM-X failed for session %s", session_id)
        if isinstance(exc, WorkerUnavailable):
            failed_at = "gpu_unavailable"
        write_latest_pointer(
            data_dir,
            session_dir=session_dir,
            session_id=session_id,
            output_dir=session_dir,
            hpe_results=Path(),
            fps=fps,
            status="error",
            error=str(exc),
            failed_at=failed_at,
        )
        return PipelineResult(
            session_id=session_id,
            video_path=session_dir / "jpegs",
            output_dir=session_dir,
            hpe_results=Path(),
            fps=fps,
            ok=False,
            error=str(exc),
            failed_at=failed_at,
        )


class GemxPipelineQueue:
    """Serialize GEM-X jobs so the GPU is not oversubscribed."""

    def __init__(
        self,
        data_dir: Path,
        *,
        on_complete: Callable[[PipelineResult], None] | None = None,
        wait_for_warm: bool = True,
    ) -> None:
        self.data_dir = data_dir
        self.on_complete = on_complete
        self._cv = threading.Condition()
        self._pending: list[Path] = []
        self._seen: set[str] = set()
        self._running: set[str] = set()  # session ids being processed right now
        self._ready = threading.Event()
        # One local GPU = one job at a time. With CEREBEL_GPU_BACKEND=modal each job gets its
        # own autoscaled GPU container, so several jobs can run side by side.
        default_workers = "10" if GPU_BACKEND == "modal" else "1"
        # deploy/env.product.example ships "CEREBEL_QUEUE_WORKERS=" (empty): that means the default.
        self.workers = max(1, int(os.environ.get("CEREBEL_QUEUE_WORKERS") or default_workers))
        for n in range(self.workers):
            threading.Thread(target=self._loop, name=f"gemx-queue-{n}", daemon=True).start()
        if GPU_BACKEND == "modal":
            wait_for_warm = False
        if wait_for_warm:
            threading.Thread(
                target=self._wait_ready, name="gemx-wait-ready", daemon=True
            ).start()
        else:
            self._ready.set()

    def _wait_ready(self) -> None:
        try:
            wait_for_worker()
        except Exception:
            LOG.exception("warm worker wait failed; jobs will retry on submit")
        finally:
            self._ready.set()

    def jobs_ahead(self, session_id: str) -> tuple[int, bool] | None:
        """(videos ahead of this one, is_running_now); None when it is not queued here."""
        with self._cv:
            if session_id in self._running:
                return 0, True
            for i, pending in enumerate(self._pending):
                if pending.name == session_id:
                    return i, False
        return None

    def enqueue(self, session_dir: Path) -> None:
        session_dir = Path(session_dir)
        with self._cv:
            key = str(session_dir.resolve())
            if key in self._seen or session_dir in self._pending:
                LOG.info("GEM-X already queued/ran for %s", session_dir.name)
                return
            self._pending.append(session_dir)
            LOG.info("queued GEM-X for session %s (depth=%s)", session_dir.name, len(self._pending))
            self._cv.notify()

    def _loop(self) -> None:
        self._ready.wait()
        while True:
            with self._cv:
                while not self._pending:
                    self._cv.wait()
                session_dir = self._pending.pop(0)
                self._running.add(session_dir.name)
            try:
                result = process_session(session_dir, self.data_dir)
            except Exception as exc:  # noqa: BLE001 - an escaped error must not end this worker thread
                LOG.exception("GEM-X job crashed for session %s", session_dir.name)
                result = PipelineResult(
                    session_id=session_dir.name, video_path=session_dir, output_dir=session_dir,
                    hpe_results=Path(), fps=0.0, ok=False, error=str(exc), failed_at="gemx",
                )
            finally:
                with self._cv:
                    self._running.discard(session_dir.name)
            with self._cv:
                self._seen.add(str(session_dir.resolve()))
            if self.on_complete is not None:
                try:
                    self.on_complete(result)
                except Exception:
                    LOG.exception("on_complete failed")
