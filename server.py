#!/usr/bin/env python3
"""CeRebel Playground capture receiver.

Binds gRPC CaptureService on CEREBEL_HOST:CEREBEL_PORT (default 0.0.0.0:61016).
Public ingress for this deployment: mars.onethingrobot.com:23210 (plaintext).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from concurrent import futures
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import grpc

from capture_pb2 import (
    CaptureAck,
    CapturePacket,
    PingRequest,
    PingResponse,
    SessionHeader,
    VideoFrame,
)
from capture_pb2_grpc import CaptureServiceServicer, add_CaptureServiceServicer_to_server
from gemx_pipeline import GemxPipelineQueue

SERVER_VERSION = "0.3.0"
DEFAULT_HOST = os.environ.get("CEREBEL_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("CEREBEL_PORT", "61016"))
DATA_DIR = Path(os.environ.get("CEREBEL_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
MAX_MESSAGE_BYTES = int(os.environ.get("CEREBEL_MAX_MESSAGE_BYTES", str(8 * 1024 * 1024)))
GEMX_ENABLED = os.environ.get("CEREBEL_GEMX_ENABLED", "1").lower() not in ("0", "false", "no")

PIXEL_FORMAT_JPEG = 3
LOG = logging.getLogger("cerebel.server")

# Populated in serve(); used to kick GEM-X after each finished recording.
_GEMX_QUEUE: GemxPipelineQueue | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _header_dict(header: SessionHeader) -> dict:
    return {
        "session_id": header.session_id,
        "device_model": header.device_model,
        "os_version": header.os_version,
        "app_version": header.app_version,
        "width": header.width,
        "height": header.height,
        "target_fps": header.target_fps,
        "pixel_format": header.pixel_format,
        "camera_position": header.camera_position,
        "synthetic_source": header.synthetic_source,
        "intrinsics": {
            "fx": header.intrinsics.fx,
            "fy": header.intrinsics.fy,
            "cx": header.intrinsics.cx,
            "cy": header.intrinsics.cy,
        },
        "received_at": _utc_now(),
    }


class SessionStore:
    def __init__(self, root: Path, session_id: str) -> None:
        self.session_id = session_id
        self.dir = root / "sessions" / session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._bin = (self.dir / "frames.bin").open("ab")
        self._index = (self.dir / "index.jsonl").open("a", encoding="utf-8")
        self._lock = threading.Lock()
        self.frames = 0
        self.bytes_received = 0

    def write_header(self, header: SessionHeader) -> None:
        path = self.dir / "header.json"
        path.write_text(json.dumps(_header_dict(header), indent=2) + "\n", encoding="utf-8")

    def write_frame(self, frame: VideoFrame) -> None:
        payload = bytes(frame.plane0) + bytes(frame.plane1)
        with self._lock:
            offset = self._bin.tell()
            self._bin.write(payload)
            self._bin.flush()
            if frame.pixel_format == PIXEL_FORMAT_JPEG:
                jpeg_dir = self.dir / "jpegs"
                jpeg_dir.mkdir(parents=True, exist_ok=True)
                (jpeg_dir / f"{frame.frame_index:06d}.jpg").write_bytes(bytes(frame.plane0))
            record = {
                "frame_index": frame.frame_index,
                "timestamp_ns": frame.timestamp_ns,
                "width": frame.width,
                "height": frame.height,
                "pixel_format": frame.pixel_format,
                "stride0": frame.stride0,
                "stride1": frame.stride1,
                "rotation_degrees": frame.rotation_degrees,
                "offset": offset,
                "size": len(payload),
                "plane0_size": len(frame.plane0),
                "plane1_size": len(frame.plane1),
                "intrinsics": {
                    "fx": frame.intrinsics.fx,
                    "fy": frame.intrinsics.fy,
                    "cx": frame.intrinsics.cx,
                    "cy": frame.intrinsics.cy,
                },
                "motion": {
                    "qx": frame.motion.qx,
                    "qy": frame.motion.qy,
                    "qz": frame.motion.qz,
                    "qw": frame.motion.qw,
                    "gravity_x": frame.motion.gravity_x,
                    "gravity_y": frame.motion.gravity_y,
                    "gravity_z": frame.motion.gravity_z,
                },
            }
            self._index.write(json.dumps(record) + "\n")
            self._index.flush()
            self.frames += 1
            self.bytes_received += len(payload)

    def close(self) -> None:
        with self._lock:
            self._bin.close()
            self._index.close()
            summary = {
                "session_id": self.session_id,
                "frames": self.frames,
                "bytes_received": self.bytes_received,
                "closed_at": _utc_now(),
            }
            (self.dir / "summary.json").write_text(
                json.dumps(summary, indent=2) + "\n", encoding="utf-8"
            )


class CaptureServicer(CaptureServiceServicer):
    def Ping(self, request: PingRequest, context: grpc.ServicerContext) -> PingResponse:
        LOG.info("ping from %s", context.peer())
        return PingResponse(server_version=SERVER_VERSION)

    def StreamCapture(
        self, request_iterator: Iterator[CapturePacket], context: grpc.ServicerContext
    ) -> CaptureAck:
        peer = context.peer()
        started = time.time()
        store: SessionStore | None = None
        session_id = f"anon-{int(time.time() * 1000)}"
        LOG.info("stream open from %s", peer)
        try:
            for packet in request_iterator:
                kind = packet.WhichOneof("payload")
                if kind == "header":
                    header = packet.header
                    session_id = header.session_id or session_id
                    if store is None:
                        store = SessionStore(DATA_DIR, session_id)
                    store.write_header(header)
                    LOG.info(
                        "session %s header %sx%s fps=%s format=%s synthetic=%s",
                        session_id,
                        header.width,
                        header.height,
                        header.target_fps,
                        header.pixel_format,
                        header.synthetic_source,
                    )
                elif kind == "frame":
                    if store is None:
                        store = SessionStore(DATA_DIR, session_id)
                    store.write_frame(packet.frame)
                elif kind == "block":
                    if store is None:
                        store = SessionStore(DATA_DIR, session_id)
                    for frame in packet.block.frames:
                        store.write_frame(frame)
                else:
                    LOG.warning("ignoring empty capture packet from %s", peer)
        except Exception:
            LOG.exception("stream failed for session %s", session_id)
            raise
        finally:
            frames = store.frames if store else 0
            nbytes = store.bytes_received if store else 0
            session_dir = store.dir if store is not None else None
            if store is not None:
                store.close()
            elapsed = max(time.time() - started, 1e-6)
            LOG.info(
                "stream closed session=%s frames=%s bytes=%s rate=%.1f fps peer=%s",
                session_id,
                frames,
                nbytes,
                frames / elapsed,
                peer,
            )
            if (
                GEMX_ENABLED
                and _GEMX_QUEUE is not None
                and session_dir is not None
                and frames > 0
            ):
                LOG.info("enqueue GEM-X pipeline for session %s", session_id)
                _GEMX_QUEUE.enqueue(session_dir)

        return CaptureAck(
            session_id=session_id,
            frames_received=store.frames if store else 0,
            bytes_received=store.bytes_received if store else 0,
        )


def _server_options() -> list[tuple[str, int | bool]]:
    return [
        ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
        ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
        ("grpc.keepalive_time_ms", 20_000),
        ("grpc.keepalive_timeout_ms", 10_000),
        ("grpc.http2.min_recv_ping_interval_without_data_ms", 5_000),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.so_reuseport", 0),
    ]


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    global _GEMX_QUEUE
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if GEMX_ENABLED:
        _GEMX_QUEUE = GemxPipelineQueue(DATA_DIR)
        LOG.info("GEM-X post-capture pipeline enabled (queue ready)")
    else:
        _GEMX_QUEUE = None
        LOG.info("GEM-X post-capture pipeline disabled")

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=16),
        options=_server_options(),
    )
    add_CaptureServiceServicer_to_server(CaptureServicer(), server)
    bind = f"{host}:{port}"
    bound = server.add_insecure_port(bind)
    if bound == 0:
        raise RuntimeError(f"failed to bind {bind}")
    server.start()
    LOG.info(
        "CeRebel capture server %s listening on %s (public mars.onethingrobot.com:23210)",
        SERVER_VERSION,
        bind,
    )

    stop = threading.Event()

    def _handle_stop(signum: int, _frame: object) -> None:
        LOG.info("received signal %s, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    stop.wait()
    server.stop(grace=5)
    LOG.info("server stopped")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("CEREBEL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        serve()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
