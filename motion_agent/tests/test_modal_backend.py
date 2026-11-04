"""CEREBEL_GPU_BACKEND=modal without Modal: every call into the `modal` package is a fake here.

No Modal account, token or network is used. Covers the web-server side of the switch (health,
upload check, the call, results, errors, timeouts, the queue, progress) and the deploy file's
torch.hub redirect that keeps a cold GPU container off github.com.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tarfile
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


def _archive(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeCall:
    def __init__(self, result=None, error: BaseException | None = None, delay: float = 0.0):
        self.result, self.error, self.delay = result, error, delay
        self.object_id = "fc-test"
        self.cancelled = False
        self.get_timeout = None

    def get(self, timeout=None):
        self.get_timeout = timeout
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result

    def cancel(self):
        self.cancelled = True


def fake_modal(*, call: FakeCall | None = None, spawn_error: BaseException | None = None,
               hydrate_error: BaseException | None = None, hydrate_delay: float = 0.0):
    """A stand-in `modal` module recording what the server asked of it."""
    mod = types.ModuleType("modal")
    mod.spawned = []
    mod.hydrated = []

    class _Run:
        def spawn(self, *args):
            if spawn_error is not None:
                raise spawn_error
            mod.spawned.append(args)
            return call

        def remote(self, *args):  # the server must not block on .remote() any more
            raise AssertionError("use spawn() + get(timeout)")

    class _Obj:
        run = _Run()

    class _Cls:
        def __init__(self, app, name):
            self.app, self.name = app, name

        def __call__(self):
            return _Obj()

        def hydrate(self):
            mod.hydrated.append((self.app, self.name))
            if hydrate_delay:
                time.sleep(hydrate_delay)
            if hydrate_error is not None:
                raise hydrate_error
            return self

    mod.Cls = types.SimpleNamespace(from_name=lambda app, name: _Cls(app, name))
    return mod


class _ModalCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.patches = [
            mock.patch.object(gemx_pipeline, "GPU_BACKEND", "modal"),
            mock.patch.object(gemx_pipeline, "DEFAULT_OUTPUT_ROOT", self.tmp / "gemx-out"),
            mock.patch.dict(gemx_pipeline._MODAL_STATUS, {"state": None, "at": float("-inf"), "error": None}),
            mock.patch.dict(os.environ, {"MODAL_TOKEN_ID": "ak-test", "MODAL_TOKEN_SECRET": "as-test"}),
            # The CPU steps need the GEM-X venv; here they are no-ops.
            mock.patch.object(gemx_pipeline, "_run_ik_subprocess", self._fake_ik),
            mock.patch.object(gemx_pipeline, "_sanitize_hpe", lambda hpe: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    @staticmethod
    def _fake_ik(session_dir, hpe, fps):
        motion = session_dir / "ik" / "opensim-motion.json"
        motion.parent.mkdir(parents=True, exist_ok=True)
        motion.write_text("{}")
        return True, motion, None

    def _session(self, sid: str, *, jpegs: int = 0) -> Path:
        session = self.tmp / "sessions" / sid
        (session / "upload").mkdir(parents=True)
        (session / "upload" / "source_video.mp4").write_bytes(b"v" * 2048)
        (session / "header.json").write_text(json.dumps({"target_fps": 30}))
        if jpegs:
            (session / "jpegs").mkdir()
            for i in range(jpegs):
                (session / "jpegs" / f"{i:06d}.jpg").write_bytes(b"j")
        return session

    def _ok_call(self) -> FakeCall:
        return FakeCall({
            "ok": True,
            "archive": _archive({"hpe_results.pt": b"pt", "preprocess/vitpose.pt": b"kp"}),
            "hpe_rel": "hpe_results.pt",
            "frames": 90,
            "elapsed_s": 41.0,
        })


class ModalStatusTests(_ModalCase):
    def test_no_token_is_down_without_calling_modal(self):
        fake = fake_modal()
        with mock.patch.dict(os.environ, {"MODAL_TOKEN_ID": "", "MODAL_TOKEN_SECRET": "",
                                          "MODAL_CONFIG_PATH": str(self.tmp / "none.toml")}), \
                mock.patch.dict(sys.modules, {"modal": fake}):
            self.assertEqual(gemx_pipeline.worker_status(), "down")
        self.assertEqual(fake.hydrated, [])
        self.assertIn("token", gemx_pipeline._MODAL_STATUS["error"])

    def test_token_in_modal_toml_counts(self):
        cfg = self.tmp / ".modal.toml"
        cfg.write_text("[default]\n")
        with mock.patch.dict(os.environ, {"MODAL_TOKEN_ID": "", "MODAL_CONFIG_PATH": str(cfg)}), \
                mock.patch.dict(sys.modules, {"modal": fake_modal()}):
            self.assertEqual(gemx_pipeline.worker_status(), "remote")

    def test_deployed_class_is_remote_and_cached(self):
        fake = fake_modal()
        with mock.patch.dict(sys.modules, {"modal": fake}):
            self.assertEqual(gemx_pipeline.worker_status(), "remote")
            self.assertEqual(gemx_pipeline.worker_status(), "remote")
            self.assertEqual(product_api.gpu_status(), "remote")
        self.assertEqual(fake.hydrated, [("cerebel-gemx", "Gemx")])  # one lookup, then the cache

    def test_lookup_failure_is_down_and_retried_soon(self):
        fake = fake_modal(hydrate_error=RuntimeError("App 'cerebel-gemx' not found"))
        with mock.patch.dict(sys.modules, {"modal": fake}):
            self.assertEqual(gemx_pipeline.worker_status(), "down")
            self.assertIn("not found", gemx_pipeline._MODAL_STATUS["error"])
            # A "down" answer is kept only briefly (30 s), a "remote" one for MODAL_STATUS_TTL_S.
            gemx_pipeline._MODAL_STATUS["at"] -= gemx_pipeline._MODAL_DOWN_TTL_S + 1
            self.assertEqual(gemx_pipeline.worker_status(), "down")
        self.assertEqual(len(fake.hydrated), 2)

    def test_modal_not_installed_is_down(self):
        with mock.patch.dict(sys.modules, {"modal": None}):  # import modal -> ImportError
            self.assertEqual(gemx_pipeline.worker_status(), "down")
        self.assertIn("modal", gemx_pipeline._MODAL_STATUS["error"])

    def test_slow_lookup_does_not_hang_health(self):
        fake = fake_modal(hydrate_delay=2.0)
        with mock.patch.dict(sys.modules, {"modal": fake}), \
                mock.patch.object(gemx_pipeline, "MODAL_STATUS_TIMEOUT_S", 0.2):
            started = time.monotonic()
            self.assertEqual(gemx_pipeline.worker_status(), "down")
            self.assertLess(time.monotonic() - started, 1.5)

    def test_down_refuses_uploads_and_fails_health(self):
        from motion_agent.app.health import health_report
        from motion_agent.tests.test_gpu_worker import _settings

        with mock.patch.dict(sys.modules, {"modal": fake_modal(hydrate_error=OSError("unreachable"))}):
            code, payload = health_report(_settings(self.tmp))
        self.assertEqual(code, 503)
        self.assertEqual(payload["gpu"], {"ok": False, "state": "down"})


class ModalRunTests(_ModalCase):
    def test_success_unpacks_result_and_runs_cpu_steps_here(self):
        session = self._session("SIDM")
        call = self._ok_call()
        fake = fake_modal(call=call)
        with mock.patch.dict(sys.modules, {"modal": fake}):
            result = gemx_pipeline.process_session(session, self.tmp)
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.ik_ok)
        sid, video, suffix, fps = fake.spawned[0]
        self.assertEqual((sid, video, suffix, fps), ("SIDM", b"v" * 2048, ".mp4", 30.0))
        self.assertEqual(call.get_timeout, gemx_pipeline.MODAL_CALL_TIMEOUT_S)
        out = self.tmp / "gemx-out" / "SIDM"
        self.assertEqual(result.hpe_results, out / "hpe_results.pt")
        self.assertTrue((out / "preprocess" / "vitpose.pt").is_file())
        manifest = json.loads((session / "gemx.json").read_text())
        self.assertEqual((manifest["mode"], manifest["input"]), ("modal", "video"))
        own = json.loads((session / gemx_pipeline.SESSION_STATUS).read_text())
        self.assertEqual((own["status"], own["ik_status"]), ("ready", "ready"))
        self.assertEqual(gemx_pipeline._MODAL_STATUS["state"], "remote")

    def test_jpegs_ready_first_still_sends_the_video(self):
        session = self._session("SIDJ", jpegs=gemx_pipeline.MIN_FRAMES)
        fake = fake_modal(call=self._ok_call())
        with mock.patch.dict(sys.modules, {"modal": fake}):
            result = gemx_pipeline.process_session(session, self.tmp)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(fake.spawned[0][1], b"v" * 2048)
        self.assertEqual(json.loads((session / "gemx.json").read_text())["input"], "video")

    def test_unreachable_modal_is_gpu_unavailable(self):
        session = self._session("SIDU")
        fake = fake_modal(spawn_error=ConnectionError("api.modal.com unreachable"))
        with mock.patch.dict(sys.modules, {"modal": fake}):
            result = gemx_pipeline.process_session(session, self.tmp)
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_at, "gpu_unavailable")
        # The next upload is refused up front instead of failing after the upload.
        self.assertEqual(gemx_pipeline._MODAL_STATUS["state"], "down")

    def test_modal_not_installed_is_gpu_unavailable(self):
        session = self._session("SIDI")
        with mock.patch.dict(sys.modules, {"modal": None}):
            result = gemx_pipeline.process_session(session, self.tmp)
        self.assertEqual(result.failed_at, "gpu_unavailable")

    def test_job_error_on_the_gpu(self):
        session = self._session("SIDE")
        fake = fake_modal(call=FakeCall({"ok": False, "error": "no person detected"}))
        with mock.patch.dict(sys.modules, {"modal": fake}):
            result = gemx_pipeline.process_session(session, self.tmp)
        self.assertEqual((result.ok, result.failed_at, result.error), (False, "gemx", "no person detected"))

    def test_remote_exception_is_a_gemx_failure(self):
        session = self._session("SIDR")
        fake = fake_modal(call=FakeCall(error=RuntimeError("CUDA out of memory")))
        with mock.patch.dict(sys.modules, {"modal": fake}):
            result = gemx_pipeline.process_session(session, self.tmp)
        self.assertEqual(result.failed_at, "gemx")
        self.assertIn("CUDA out of memory", result.error)

    def test_timeout_cancels_the_call(self):
        session = self._session("SIDT")

        class FunctionTimeoutError(Exception):  # the name Modal uses
            pass

        for error in (TimeoutError(), FunctionTimeoutError("20 min")):
            call = FakeCall(error=error)
            with mock.patch.dict(sys.modules, {"modal": fake_modal(call=call)}):
                result = gemx_pipeline.process_session(session, self.tmp)
            self.assertEqual(result.failed_at, "gemx")
            self.assertIn("timed out", result.error)
            self.assertTrue(call.cancelled)

    def test_unsafe_archive_is_rejected(self):
        session = self._session("SIDX")
        call = FakeCall({"ok": True, "archive": _archive({"../escape.pt": b"x"}), "hpe_rel": "hpe_results.pt"})
        with mock.patch.dict(sys.modules, {"modal": fake_modal(call=call)}):
            result = gemx_pipeline.process_session(session, self.tmp)
        self.assertFalse(result.ok)
        self.assertIn("unsafe path", result.error)
        self.assertFalse((self.tmp / "gemx-out" / "escape.pt").exists())


class ModalQueueTests(_ModalCase):
    def test_defaults_parallel_and_no_local_worker_wait(self):
        with mock.patch.dict(os.environ, {"CEREBEL_QUEUE_WORKERS": ""}), \
                mock.patch.object(gemx_pipeline, "wait_for_worker", side_effect=AssertionError("no local worker")):
            q = gemx_pipeline.GemxPipelineQueue(self.tmp, wait_for_warm=True)
        self.assertEqual(q.workers, 10)
        self.assertTrue(q._ready.is_set())

    def test_empty_queue_workers_means_default_locally_too(self):
        # "CEREBEL_QUEUE_WORKERS=" straight from env.product.example used to crash with ValueError.
        with mock.patch.object(gemx_pipeline, "GPU_BACKEND", "local"), \
                mock.patch.dict(os.environ, {"CEREBEL_QUEUE_WORKERS": ""}):
            q = gemx_pipeline.GemxPipelineQueue(self.tmp, wait_for_warm=False)
        self.assertEqual(q.workers, 1)

    def test_a_crashing_job_does_not_stop_the_worker(self):
        done = []
        calls = []

        def process(session_dir, data_dir):
            calls.append(session_dir.name)
            if session_dir.name == "BAD":
                raise OSError("disk full")
            return gemx_pipeline.PipelineResult(session_id=session_dir.name, video_path=session_dir,
                                                output_dir=session_dir, hpe_results=session_dir, fps=30.0, ok=True)

        with mock.patch.object(gemx_pipeline, "process_session", process), \
                mock.patch.dict(os.environ, {"CEREBEL_QUEUE_WORKERS": "1"}):
            q = gemx_pipeline.GemxPipelineQueue(self.tmp, on_complete=done.append, wait_for_warm=False)
            q.enqueue(self.tmp / "BAD")
            q.enqueue(self.tmp / "GOOD")
            deadline = time.time() + 5
            while len(done) < 2 and time.time() < deadline:
                time.sleep(0.02)
        self.assertEqual(calls, ["BAD", "GOOD"])
        self.assertEqual([(r.session_id, r.ok, r.failed_at) for r in done],
                         [("BAD", False, "gemx"), ("GOOD", True, None)])

    def test_cpu_post_processing_is_capped(self):
        running, peak, lock = [0], [0], threading.Lock()

        def slow_ik(session_dir, hpe, fps):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.2)
            with lock:
                running[0] -= 1
            return self._fake_ik(session_dir, hpe, fps)

        sessions = [self._session(f"SIDP{i}") for i in range(4)]
        fake = fake_modal(call=self._ok_call())
        with mock.patch.dict(sys.modules, {"modal": fake}), \
                mock.patch.object(gemx_pipeline, "_run_ik_subprocess", slow_ik), \
                mock.patch.object(gemx_pipeline, "_POSTPROCESS_SLOTS", threading.BoundedSemaphore(2)):
            threads = [threading.Thread(target=gemx_pipeline.process_session, args=(s, self.tmp)) for s in sessions]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(peak[0], 2)
        self.assertTrue(all((s / "ik" / "opensim-motion.json").is_file() for s in sessions))


class ParallelProgressTests(_ModalCase):
    def test_each_session_reads_its_own_state(self):
        a, b = self._session("SIDA"), self._session("SIDB")
        common = dict(output_dir=self.tmp, hpe_results=Path(), fps=30.0)
        gemx_pipeline.write_latest_pointer(self.tmp, session_dir=a, session_id="SIDA", status="running",
                                           ik_status="running", **common)
        gemx_pipeline.write_latest_pointer(self.tmp, session_dir=b, session_id="SIDB", status="running", **common)
        # latest_mesh.json now only knows about B; A's step is not lost.
        self.assertEqual(gemx_pipeline.read_session_pointer(self.tmp, "SIDA")["ik_status"], "running")
        self.assertEqual(gemx_pipeline.read_session_pointer(self.tmp, "SIDB")["session_id"], "SIDB")
        self.assertIsNone(gemx_pipeline.read_session_pointer(self.tmp, "NOPE"))

    def test_old_sessions_fall_back_to_latest_mesh(self):
        legacy = self.tmp / "sessions" / "OLD"
        legacy.mkdir(parents=True)
        gemx_pipeline.write_latest_pointer(self.tmp, session_id="OLD", output_dir=legacy,
                                           hpe_results=Path(), fps=30.0, status="running")
        self.assertFalse((legacy / gemx_pipeline.SESSION_STATUS).exists())
        self.assertEqual(gemx_pipeline.read_session_pointer(self.tmp, "OLD")["status"], "running")

    def test_upload_watcher_sees_its_own_step(self):
        import upload_server

        a = self._session("SIDW")
        common = dict(output_dir=self.tmp, hpe_results=Path(), fps=30.0)
        gemx_pipeline.write_latest_pointer(self.tmp, session_dir=a, session_id="SIDW", status="error",
                                           error="boom", failed_at="gemx", **common)
        other = self._session("SIDO")
        gemx_pipeline.write_latest_pointer(self.tmp, session_dir=other, session_id="SIDO", status="running", **common)
        with mock.patch.object(upload_server, "DATA_DIR", self.tmp):
            upload_server._write_progress("SIDW", stage="queued")
            upload_server._watch_pipeline("SIDW")  # returns once it has mirrored the error
            prog = upload_server._read_progress("SIDW")
        for sid in ("SIDW",):
            upload_server._JOBS.pop(sid, None)
        self.assertEqual((prog["stage"], prog["failed_at"]), ("error", "gemx"))


def _load_modal_app(env: dict | None = None):
    """Import deploy/modal_gemx.py with a mock `modal`, the way `modal deploy` would read it."""
    spec = importlib.util.spec_from_file_location("modal_gemx_under_test", ROOT / "deploy" / "modal_gemx.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"modal": mock.MagicMock()}), mock.patch.dict(os.environ, env or {}):
        spec.loader.exec_module(module)
    return module


class ModalAppTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.calls = []

        def original_load(repo_or_dir, model, *args, source="github", **kwargs):
            self.calls.append((repo_or_dir, model, source, kwargs))
            return "model"

        self.torch = types.ModuleType("torch")
        self.torch.hub = types.SimpleNamespace(load=original_load)

    def tearDown(self):
        self._tmp.cleanup()

    def _cache_dinov3(self, app) -> Path:
        repo = app._hub_cache_dir("facebookresearch/dinov3", str(self.home))
        repo.mkdir(parents=True)
        (repo / "hubconf.py").write_text("")
        return repo

    def test_cached_dinov3_never_goes_to_github(self):
        app = _load_modal_app()
        repo = self._cache_dinov3(app)
        self.assertEqual(repo, self.home / "hub" / "facebookresearch_dinov3_main")
        with mock.patch.dict(sys.modules, {"torch": self.torch}):
            info = app._use_cached_hub_repos(str(self.home))
            # The two upstream call sites (ViTPose extractor, SAM-3D-Body backbone):
            self.torch.hub.load("facebookresearch/dinov3", "dinov3_vits16", source="github",
                                pretrained=False, skip_validation=True)
            self.torch.hub.load("facebookresearch/dinov3", "dinov3_vith16plus", source="github",
                                pretrained=False, drop_path=0.1)
            self.torch.hub.load("pytorch/vision", "resnet18", source="github")  # anything else: untouched
            app._use_cached_hub_repos(str(self.home))  # a second call does not wrap twice
            self.torch.hub.load("facebookresearch/dinov3:main", "dinov3_vits16")
        self.assertEqual(info, {"facebookresearch/dinov3": str(repo)})
        self.assertEqual([c[:3] for c in self.calls], [
            (str(repo), "dinov3_vits16", "local"),
            (str(repo), "dinov3_vith16plus", "local"),
            ("pytorch/vision", "resnet18", "github"),
            (str(repo), "dinov3_vits16", "local"),
        ])
        self.assertEqual(self.calls[1][3], {"pretrained": False, "drop_path": 0.1})

    def test_missing_cache_is_reported(self):
        app = _load_modal_app()
        with mock.patch.dict(sys.modules, {"torch": self.torch}):
            info = app._use_cached_hub_repos(str(self.home))
            self.torch.hub.load("facebookresearch/dinov3", "dinov3_vits16", source="github")
        self.assertIn("prefetch", info["facebookresearch/dinov3"])
        self.assertEqual(self.calls[0][2], "github")

    def test_cache_lives_in_the_volume(self):
        app = _load_modal_app()
        self.assertEqual(app.TORCH_HOME, "/cache/torch")
        self.assertIn("/cache", app.VOLUMES)

    def test_region_must_stay_in_the_us(self):
        self.assertEqual(_load_modal_app({"CEREBEL_MODAL_REGION": "us-east,us-west"}).REGION, ["us-east", "us-west"])
        self.assertEqual(_load_modal_app({"CEREBEL_MODAL_REGION": ""}).REGION, ["us"])
        with self.assertRaises(SystemExit):
            _load_modal_app({"CEREBEL_MODAL_REGION": "us,eu-west"})
        with self.assertRaises(SystemExit):
            _load_modal_app({"CEREBEL_MODAL_REGION": "ap-northeast"})


if __name__ == "__main__":
    unittest.main()
