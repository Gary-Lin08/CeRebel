"""GPU worker down or restarting: fail fast at upload, survive a restart, say where it failed.

Regression for the 2026-10-24 pipeline_error events: the warm GEM-X worker was not running,
every upload waited 10 minutes in the queue and then failed with "[Errno 111] Connection refused".
"""

from __future__ import annotations

import email.message
import io
import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gemx_pipeline  # noqa: E402

from motion_agent.app import product_api  # noqa: E402
from motion_agent.app.config import Settings  # noqa: E402
from motion_agent.app.health import health_report  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeWorker(socketserver.ThreadingTCPServer):
    """Speaks the warm worker's one-JSON-line protocol."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port: int, out_root: Path, *, ready: bool = True) -> None:
        self.ready = ready
        self.out_root = out_root
        self.runs: list[str] = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(inner) -> None:  # noqa: N805
                req = json.loads(inner.rfile.readline().decode("utf-8"))
                if req.get("cmd") == "ping":
                    reply = {"ok": True, "ready": self.ready, "loading": not self.ready}
                else:
                    out = self.out_root / req["session_id"]
                    out.mkdir(parents=True, exist_ok=True)
                    (out / "hpe_results.pt").write_bytes(b"x")
                    self.runs.append(req["session_id"])
                    reply = {"ok": True, "output_dir": str(out), "hpe_results": str(out / "hpe_results.pt")}
                inner.wfile.write((json.dumps(reply) + "\n").encode("utf-8"))

        super().__init__(("127.0.0.1", port), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


def _settings(root: Path) -> Settings:
    return Settings(
        data_dir=root,
        host="127.0.0.1",
        port=1,
        backend="mock",
        prompt_password=None,
        max_upload_bytes=10_000_000,
        invite_daily_limit=3,
    )


def _events(settings: Settings) -> list[dict]:
    path = settings.product_dir / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _WorkerPort(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.port = _free_port()
        self.patches = [
            mock.patch.object(gemx_pipeline, "WORKER_PORT", self.port),
            mock.patch.object(gemx_pipeline, "GPU_BACKEND", "local"),
            mock.patch.object(gemx_pipeline, "DEFAULT_OUTPUT_ROOT", self.tmp / "gemx-out"),
            mock.patch.object(gemx_pipeline, "_WAIT_GAVE_UP_AT", float("-inf")),
        ]
        for p in self.patches:
            p.start()
        self.workers: list[_FakeWorker] = []

    def tearDown(self):
        for w in self.workers:
            w.stop()
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def _worker(self, **kw) -> _FakeWorker:
        w = _FakeWorker(self.port, self.tmp / "gemx-out", **kw)
        self.workers.append(w)
        return w


class WorkerStatusTests(_WorkerPort):
    def test_states(self):
        self.assertEqual(gemx_pipeline.worker_status(timeout=1), "down")
        w = self._worker(ready=False)
        self.assertEqual(gemx_pipeline.worker_status(timeout=1), "loading")
        w.ready = True
        self.assertEqual(gemx_pipeline.worker_status(timeout=1), "ready")
        with mock.patch.object(gemx_pipeline, "GPU_BACKEND", "modal"):
            self.assertEqual(gemx_pipeline.worker_status(timeout=1), "remote")


class SubmitRetryTests(_WorkerPort):
    def test_job_survives_a_worker_restart(self):
        # Worker comes back (supervisor restart) while the job waits: the job still runs.
        threading.Timer(0.5, self._worker).start()
        with mock.patch.object(gemx_pipeline, "WORKER_SUBMIT_WAIT_S", 10):
            out = gemx_pipeline.run_gemx_warm("SID1", 30.0, video_path=self.tmp / "v.mp4")
        self.assertTrue((out / "hpe_results.pt").is_file())
        self.assertEqual(self.workers[0].runs, ["SID1"])

    def test_worker_never_comes_back(self):
        session = self.tmp / "sessions" / "SID2"
        (session / "upload").mkdir(parents=True)
        (session / "upload" / "source_video.mp4").write_bytes(b"x" * 2048)
        with mock.patch.object(gemx_pipeline, "WORKER_SUBMIT_WAIT_S", 0.5):
            result = gemx_pipeline.process_session(session, self.tmp)
            self.assertFalse(result.ok)
            self.assertEqual(result.failed_at, "gpu_unavailable")
            ptr = json.loads((self.tmp / "latest_mesh.json").read_text())
            self.assertEqual(ptr["status"], "error")
            self.assertEqual(ptr["failed_at"], "gpu_unavailable")

            # The next queued job does not wait all over again.
            started = time.monotonic()
            with self.assertRaises(gemx_pipeline.WorkerUnavailable):
                gemx_pipeline.run_gemx_warm("SID3", 30.0, video_path=self.tmp / "v.mp4")
            self.assertLess(time.monotonic() - started, 0.4)

    def test_bad_input_is_not_blamed_on_the_gpu(self):
        session = self.tmp / "sessions" / "EMPTY"
        session.mkdir(parents=True)
        result = gemx_pipeline.process_session(session, self.tmp)
        self.assertEqual(result.failed_at, "input")


class ProgressMessageTests(unittest.TestCase):
    def test_gpu_unavailable_progress_is_readable(self):
        import upload_server

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(upload_server, "DATA_DIR", Path(tmp)):
            result = gemx_pipeline.PipelineResult(
                session_id="SIDX", video_path=Path(tmp), output_dir=Path(tmp), hpe_results=Path(),
                fps=30.0, ok=False, error="warm worker not ready after 300s", failed_at="gpu_unavailable",
            )
            upload_server._on_pipeline_complete(result)
            prog = json.loads((Path(tmp) / "sessions" / "SIDX" / "upload_progress.json").read_text())
            upload_server._JOBS.pop("SIDX", None)
        self.assertEqual(prog["stage"], "error")
        self.assertEqual(prog["failed_at"], "gpu_unavailable")
        shown = product_api.public_progress(prog)["message"]
        self.assertEqual(shown, upload_server.GPU_UNAVAILABLE_MESSAGE)
        self.assertNotIn("Errno", shown)


def _multipart(filename: str, payload: bytes) -> tuple[email.message.Message, bytes]:
    boundary = "cerebelboundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: video/mp4\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    headers = email.message.Message()
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    headers["Content-Length"] = str(len(body))
    return headers, body


class UploadPreflightTests(unittest.TestCase):
    def _handler(self, filename: str = "swing.mp4"):
        headers, body = _multipart(filename, b"v" * 4096)
        return types.SimpleNamespace(headers=headers, rfile=io.BytesIO(body))

    def test_gpu_down_rejects_before_saving(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            with mock.patch.object(product_api, "gpu_status", return_value="down"), \
                    mock.patch.object(product_api, "save_upload") as save:
                code, payload = product_api.parse_upload(self._handler(), settings, {"user_id": "u1"})
            self.assertEqual(code, 503)
            self.assertTrue(payload["unavailable"])
            self.assertIn("didn't use one of your analyses", payload["message"])
            save.assert_not_called()
            self.assertFalse((Path(tmp) / "sessions").exists())
            self.assertEqual([e["kind"] for e in _events(settings)], ["upload_unavailable"])

    def test_gpu_loading_or_remote_still_accepts(self):
        for state in ("ready", "loading", "remote", "unknown"):
            with tempfile.TemporaryDirectory() as tmp:
                settings = _settings(Path(tmp))
                with mock.patch.object(product_api, "gpu_status", return_value=state), \
                        mock.patch.object(product_api, "save_upload", return_value=(200, {"ok": True})) as save:
                    code, _ = product_api.parse_upload(self._handler(), settings, {"user_id": "u1"})
                self.assertEqual(code, 200, state)
                self.assertEqual(save.call_args.kwargs["filename"], "swing.mp4")

    def test_gpu_status_reads_the_worker(self):
        with mock.patch.object(gemx_pipeline, "WORKER_PORT", _free_port()), \
                mock.patch.object(gemx_pipeline, "GPU_BACKEND", "local"):
            self.assertEqual(product_api.gpu_status(), "down")


class PipelineErrorEventTests(unittest.TestCase):
    def test_event_records_where_it_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))

            def fake_process(session_id, name):
                product_api._write_progress(
                    settings, session_id, stage="error", percent=100,
                    message="Our analysis server was offline.", error="[Errno 111] Connection refused",
                    failed_at="gpu_unavailable",
                )

            fake = types.ModuleType("upload_server")
            fake._process_uploaded_session = fake_process
            with mock.patch.dict(sys.modules, {"upload_server": fake}), \
                    mock.patch.object(product_api, "_email_owner"):
                product_api._run_pipeline(settings, "SIDE", "a.mp4")
            errors = [e for e in _events(settings) if e["kind"] == "pipeline_error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["failed_at"], "gpu_unavailable")
        self.assertEqual(errors[0]["message"], "[Errno 111] Connection refused")

    def test_start_failure_is_labelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            fake = types.ModuleType("upload_server")

            def boom(session_id, name):
                raise OSError("disk full")

            fake._process_uploaded_session = boom
            with mock.patch.dict(sys.modules, {"upload_server": fake}):
                product_api._run_pipeline(settings, "SIDS", "a.mp4")
            errors = [e for e in _events(settings) if e["kind"] == "pipeline_error"]
        self.assertEqual(errors[0]["failed_at"], "start")


class HealthGpuTests(unittest.TestCase):
    def test_gpu_down_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            with mock.patch.object(product_api, "gpu_status", return_value="down"):
                code, payload = health_report(settings)
            self.assertEqual(code, 503)
            self.assertEqual(payload["gpu"], {"ok": False, "state": "down"})
            for state in ("ready", "loading", "remote"):
                with mock.patch.object(product_api, "gpu_status", return_value=state):
                    code, payload = health_report(settings)
                self.assertEqual(code, 200, state)
                self.assertEqual(payload["gpu"]["state"], state)


if __name__ == "__main__":
    unittest.main()
