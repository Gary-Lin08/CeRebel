"""Plans, monthly quota, Stripe webhook handling, consent and client events."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from motion_agent.app import billing
from motion_agent.app.accounts import profile, register
from motion_agent.app.config import Settings
from motion_agent.app.library import add_capture, list_captures, update_status
from motion_agent.app.product_api import handle_auth_post, record_client_event, save_upload

SECRET = "whsec_test_secret"


def _sign(payload: bytes, secret: str = SECRET, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


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


class SignatureTests(unittest.TestCase):
    def test_valid_signature(self):
        body = b'{"id":"evt_1"}'
        self.assertTrue(billing.verify_signature(body, _sign(body), SECRET))

    def test_wrong_secret_or_body(self):
        body = b'{"id":"evt_1"}'
        self.assertFalse(billing.verify_signature(body, _sign(body, "whsec_other"), SECRET))
        self.assertFalse(billing.verify_signature(b'{"id":"evt_2"}', _sign(body), SECRET))

    def test_stale_timestamp_and_missing_header(self):
        body = b"{}"
        self.assertFalse(billing.verify_signature(body, _sign(body, ts=int(time.time()) - 3600), SECRET))
        self.assertFalse(billing.verify_signature(body, None, SECRET))
        self.assertFalse(billing.verify_signature(body, _sign(body), ""))


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.product = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": SECRET})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _send(self, event: dict) -> dict:
        raw = json.dumps(event).encode()
        return billing.handle_webhook(self.product, raw, _sign(raw))

    def test_checkout_completed_makes_user_pro(self):
        self.assertFalse(billing.is_pro(self.product, "u1"))
        self._send({
            "id": "evt_a",
            "type": "checkout.session.completed",
            "data": {"object": {
                "mode": "subscription", "client_reference_id": "u1",
                "customer": "cus_1", "subscription": "sub_1", "metadata": {"plan": "founding"},
            }},
        })
        self.assertTrue(billing.is_pro(self.product, "u1"))
        self.assertEqual(billing._read(self.product)["founding_used"], 1)

    def test_subscription_deleted_ends_pro_and_duplicates_ignored(self):
        future = int(time.time()) + 30 * 86400
        self._send({
            "id": "evt_b",
            "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_1", "customer": "cus_1", "status": "active",
                "metadata": {"user_id": "u2"},
                "items": {"data": [{"current_period_end": future}]},
            }},
        })
        self.assertTrue(billing.is_pro(self.product, "u2"))
        self.assertEqual(billing.subscription_for(self.product, "u2")["current_period_end"], future)
        deleted = {
            "id": "evt_c",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "canceled", "metadata": {}}},
        }
        self._send(deleted)
        self.assertFalse(billing.is_pro(self.product, "u2"))
        self.assertTrue(self._send(deleted).get("duplicate"))

    def test_bad_signature_rejected(self):
        raw = b'{"id":"evt_x","type":"checkout.session.completed"}'
        with self.assertRaises(billing.BillingError):
            billing.handle_webhook(self.product, raw, _sign(raw, "whsec_wrong"))

    def test_expired_period_is_not_pro(self):
        data = billing._read(self.product)
        data["users"]["u3"] = {"status": "active", "current_period_end": int(time.time()) - 3 * 86400}
        billing._write(self.product, data)
        self.assertFalse(billing.is_pro(self.product, "u3"))


class QuotaTests(unittest.TestCase):
    def test_failed_uploads_do_not_count(self):
        now = datetime(2026, 11, 15, tzinfo=timezone.utc)
        rows = [
            {"created_at": "2026-11-02T10:00:00+00:00", "status": "ready"},
            {"created_at": "2026-11-03T10:00:00+00:00", "status": "processing"},
            {"created_at": "2026-11-04T10:00:00+00:00", "status": "error"},
            {"created_at": "2026-11-05T10:00:00+00:00", "status": "no_swing"},
            {"created_at": "2026-10-30T10:00:00+00:00", "status": "ready"},
        ]
        self.assertEqual(billing.analyses_this_month(rows, now), 2)

    def test_upload_blocked_with_402_when_free_quota_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            for i in range(3):
                add_capture(settings.product_dir, user_id="u1", session_id=f"S{i}", filename="a.mp4")
                update_status(settings.product_dir, f"S{i}", "ready")
            code, payload = save_upload(
                settings, {"user_id": "u1", "email": "a@b.co"}, filename="swing.mp4", file_obj=io.BytesIO(b"x" * 4096)
            )
            self.assertEqual(code, 402)
            self.assertTrue(payload["paywall"])
            self.assertEqual(payload["quota"]["remaining"], 0)
            self.assertEqual(len(list_captures(settings.product_dir, "u1")), 3)

    def test_failed_capture_frees_a_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            for i in range(3):
                add_capture(settings.product_dir, user_id="u1", session_id=f"S{i}", filename="a.mp4")
            update_status(settings.product_dir, "S0", "error")
            q = billing.quota(settings.product_dir, "u1", list_captures(settings.product_dir, "u1"))
            self.assertEqual(q["remaining"], 1)
            self.assertEqual(q["plan"], "free")


class PlansTests(unittest.TestCase):
    def test_plans_hidden_until_configured(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            for key in ("STRIPE_SECRET_KEY", "STRIPE_PRICE_ANNUAL", "STRIPE_PRICE_MONTHLY", "STRIPE_FOUNDING_COUPON"):
                os.environ.pop(key, None)
            info = billing.public_plans(Path(tmp))
            self.assertFalse(info["checkout_enabled"])
            self.assertEqual(info["plans"], [])

    def test_founding_plan_offered_until_limit(self):
        env = {
            "STRIPE_SECRET_KEY": "sk_test_x",
            "STRIPE_PRICE_ANNUAL": "price_a",
            "STRIPE_FOUNDING_COUPON": "founding",
            "CEREBEL_FOUNDING_LIMIT": "1",
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env):
            root = Path(tmp)
            self.assertEqual([p["id"] for p in billing.public_plans(root)["plans"]], ["founding", "annual"])
            data = billing._read(root)
            data["founding_used"] = 1
            billing._write(root, data)
            self.assertEqual([p["id"] for p in billing.public_plans(root)["plans"]], ["annual"])

    def test_checkout_builds_stripe_request(self):
        env = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PRICE_ANNUAL": "price_a", "CEREBEL_PUBLIC_URL": "https://app.example"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env):
            with mock.patch.object(billing, "_stripe_request", return_value={"url": "https://checkout", "id": "cs_1"}) as req:
                out = billing.create_checkout(Path(tmp), {"user_id": "u1", "email": "a@b.co", "coach_code": "PRO1"}, "annual")
            self.assertEqual(out["url"], "https://checkout")
            params = req.call_args[0][2]
            self.assertEqual(params["client_reference_id"], "u1")
            self.assertEqual(params["subscription_data[metadata][coach_code]"], "PRO1")
            self.assertEqual(params["success_url"], "https://app.example/?checkout=success")


class ConsentAndEventsTests(unittest.TestCase):
    def test_register_requires_consent_over_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            code, payload = handle_auth_post(
                settings, "/api/auth/register", {"email": "a@b.co", "password": "longenough"}, {}
            )
            self.assertEqual(code, 400)
            code, payload = handle_auth_post(
                settings,
                "/api/auth/register",
                {"email": "a@b.co", "password": "longenough", "accept_terms": True, "coach_code": "coach-7!"},
                {},
            )
            self.assertEqual(code, 200)
            prof = profile(settings.product_dir, payload["user"]["user_id"])
            self.assertEqual(prof["coach_code"], "COACH-7")
            self.assertTrue(prof["terms_version"])

    def test_register_function_rejects_without_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                register(Path(tmp), "c@d.co", "longenough", accepted_terms=False)

    def test_client_events_whitelisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            self.assertEqual(record_client_event(settings, None, {"event": "hack"})[0], 400)
            code, _ = record_client_event(
                settings, {"user_id": "u9"}, {"event": "trust_yes", "props": {"user_id": "spoof", "session_id": "S1"}}
            )
            self.assertEqual(code, 200)
            row = json.loads((settings.product_dir / "events.jsonl").read_text().splitlines()[-1])
            self.assertEqual(row["kind"], "client_trust_yes")
            self.assertEqual(row["user_id"], "u9")
            self.assertEqual(row["session_id"], "S1")


if __name__ == "__main__":
    unittest.main()


class PipelineWaitTests(unittest.TestCase):
    def test_run_pipeline_waits_then_finalizes(self):
        import sys
        import types

        from motion_agent.app import product_api

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            add_capture(settings.product_dir, user_id="u1", session_id="SID1", filename="a.mp4")
            calls = {}

            def fake_process(session_id, name):
                # The GPU pipeline finishes asynchronously; simulate it writing "ready".
                product_api._write_progress(settings, session_id, stage="ready", percent=100)

            fake = types.ModuleType("upload_server")
            fake._process_uploaded_session = fake_process
            with mock.patch.dict(sys.modules, {"upload_server": fake}), mock.patch.object(
                product_api, "finalize_capture", side_effect=lambda s, sid: calls.setdefault("final", sid)
            ):
                product_api._run_pipeline(settings, "SID1", "a.mp4")
            self.assertEqual(calls.get("final"), "SID1")

    def test_wait_times_out_to_error(self):
        from motion_agent.app import product_api

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            product_api._write_progress(settings, "SID2", stage="queued", percent=20)
            with mock.patch.object(product_api, "PIPELINE_TIMEOUT_S", 0):
                prog = product_api._wait_for_pipeline(settings, "SID2", poll_s=0)
            self.assertEqual(prog["stage"], "error")


class QualityGateTests(unittest.TestCase):
    def test_quality_rules(self):
        from motion_agent.app.product_api import quality_check

        self.assertIsNone(quality_check(None)[0])
        self.assertIn("only", quality_check({"duration": 0.8, "fps": 30, "width": 1080, "height": 1920})[0])
        self.assertIn("Trim", quality_check({"duration": 95, "fps": 30, "width": 1080, "height": 1920})[0])
        self.assertIn("choppy", quality_check({"duration": 5, "fps": 15, "width": 1080, "height": 1920})[0])
        self.assertIn("too small", quality_check({"duration": 5, "fps": 30, "width": 240, "height": 320})[0])
        ok, warnings = quality_check({"duration": 5, "fps": 30, "width": 1080, "height": 1920})
        self.assertIsNone(ok)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(quality_check({"duration": 5, "fps": 240, "width": 1080, "height": 1920}), (None, []))

    def test_rejected_upload_is_free_and_deleted(self):
        from motion_agent.app import product_api

        from motion_agent.app.accounts import record_video_consent
        from motion_agent.app.consent import VIDEO_CONSENT_VERSION

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            uid = register(settings.product_dir, "a@b.co", "longenough")["user"]["user_id"]
            record_video_consent(settings.product_dir, uid, VIDEO_CONSENT_VERSION)
            bad = {"duration": 0.5, "fps": 30, "width": 1080, "height": 1920}
            with mock.patch.object(product_api, "probe_video", return_value=bad):
                code, payload = save_upload(
                    settings, {"user_id": uid, "email": "a@b.co"}, filename="s.mp4", file_obj=io.BytesIO(b"x" * 4096)
                )
            self.assertEqual(code, 422)
            self.assertTrue(payload["rejected"])
            rows = list_captures(settings.product_dir, uid)
            self.assertEqual(rows[0]["status"], "rejected")
            self.assertEqual(billing.quota(settings.product_dir, uid, rows)["remaining"], 3)
            self.assertFalse((Path(tmp) / "sessions").exists() and any((Path(tmp) / "sessions").iterdir()))


class QuestionAllowanceTests(unittest.TestCase):
    def test_daily_question_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(billing.take_question(root, "u:1", 2), (True, 1))
            self.assertEqual(billing.take_question(root, "u:1", 2), (True, 0))
            self.assertEqual(billing.take_question(root, "u:1", 2), (False, 0))
            self.assertTrue(billing.take_question(root, "u:2", 2)[0])

    def test_limits_by_plan(self):
        with mock.patch.dict(os.environ, {"CEREBEL_ANON_DAILY_QUESTIONS": "2", "CEREBEL_FREE_DAILY_QUESTIONS": "5"}):
            self.assertEqual(billing.daily_question_limit(False, False), 2)
            self.assertEqual(billing.daily_question_limit(False, True), 5)
            self.assertEqual(billing.daily_question_limit(True, True), 100)


class ProviderConfigTests(unittest.TestCase):
    def test_generic_api_key_and_embed_url(self):
        from motion_agent.knowledge import DashScopeEmbedder
        from motion_agent.llm.qwen_client import _load_api_key

        env = {
            "MOTION_AGENT_API_KEY": "k-generic",
            "MOTION_AGENT_BASE_URL": "https://chat.example/v1",
            "MOTION_AGENT_KB_EMBED_BASE_URL": "https://embed.example/v1",
            "MOTION_AGENT_KB_EMBED_API_KEY": "k-embed",
        }
        with mock.patch.dict(os.environ, env):
            self.assertEqual(_load_api_key(), "k-generic")
            emb = DashScopeEmbedder()
            self.assertEqual(emb.base_url, "https://embed.example/v1")
            self.assertEqual(emb.api_key, "k-embed")


class TrimAndQueueTests(unittest.TestCase):
    def test_parse_trim_rules(self):
        from motion_agent.app.product_api import _parse_trim

        self.assertIsNone(_parse_trim(None, 20))
        self.assertEqual(_parse_trim(("4.2", "7.5"), 20), (4.2, 7.5))
        self.assertIsNone(_parse_trim(("4", "4.5"), 20))      # too short
        self.assertIsNone(_parse_trim(("0", "15"), 20))       # too long
        self.assertIsNone(_parse_trim(("0", "5.9"), 6))       # basically the whole clip
        self.assertEqual(_parse_trim(("-1", "3"), 20), (0.0, 3.0))
        self.assertIsNone(_parse_trim(("x", "3"), 20))

    def test_trim_video_with_ffmpeg(self):
        import shutil
        import subprocess

        from motion_agent.app.product_api import probe_video, trim_video

        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg not installed")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source_video.mov"
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=60",
                 "-t", "8", str(src)], check=True,
            )
            out = trim_video(src, 2.0, 5.0)
            self.assertIsNotNone(out)
            meta = probe_video(out)
            self.assertAlmostEqual(meta["duration"], 3.0, delta=0.15)
            self.assertGreater(meta["fps"], 50)

    def test_queue_info_messages(self):
        import sys
        import types

        from motion_agent.app import product_api

        class FakeQueue:
            def jobs_ahead(self, sid):
                return {"RUN": (0, True), "NEXT": (0, False), "LATER": (3, False)}.get(sid)

        fake = types.ModuleType("upload_server")
        fake._QUEUE = FakeQueue()
        with mock.patch.dict(sys.modules, {"upload_server": fake}):
            self.assertTrue(product_api.queue_info("RUN")["running"])
            self.assertIn("next", product_api.queue_info("NEXT")["queue_message"])
            later = product_api.queue_info("LATER")
            self.assertEqual(later["jobs_ahead"], 3)
            self.assertIn("3 swings ahead", later["queue_message"])
            self.assertIsNone(product_api.queue_info("NOPE"))


class NotifyTests(unittest.TestCase):
    def test_disabled_without_smtp(self):
        from motion_agent.app import notify

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CEREBEL_SMTP_HOST", None)
            self.assertFalse(notify.notify_analysis("a@b.co", "ready", "SID"))

    def test_message_contents(self):
        from motion_agent.app import notify

        env = {"CEREBEL_MAIL_FROM": "CeRebel <garylin@cerebel.tech>", "CEREBEL_PUBLIC_URL": "https://playground.cerebel.tech"}
        with mock.patch.dict(os.environ, env):
            ok = notify.build_message("a@b.co", "ready", "SID9")
            self.assertIn("ready", ok["Subject"])
            self.assertIn("https://playground.cerebel.tech/?session=SID9", ok.get_content())
            bad = notify.build_message("a@b.co", "failed", "SID9", "no swing found")
            self.assertIn("did not use one of your monthly analyses", bad.get_content())

    def test_send_runs_in_background(self):
        from motion_agent.app import notify

        env = {"CEREBEL_SMTP_HOST": "smtp.example", "CEREBEL_MAIL_FROM": "x@example.com"}
        with mock.patch.dict(os.environ, env), mock.patch.object(notify, "_send") as send:
            self.assertTrue(notify.notify_analysis("a@b.co", "ready", "SID"))
            for t in __import__("threading").enumerate():
                if t.name.startswith("mail-"):
                    t.join(2)
            send.assert_called_once()


class GpuQueueTests(unittest.TestCase):
    def test_queue_positions_and_parallel_workers(self):
        import sys
        import threading

        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import gemx_pipeline

        gate = threading.Event()
        started = []

        def fake_process(session_dir, data_dir):
            started.append(session_dir.name)
            gate.wait(5)
            return gemx_pipeline.PipelineResult(session_id=session_dir.name, video_path=session_dir, output_dir=session_dir, hpe_results=session_dir, fps=30.0, ok=True)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gemx_pipeline, "process_session", fake_process), \
                mock.patch.dict(os.environ, {"CEREBEL_QUEUE_WORKERS": "2"}):
            q = gemx_pipeline.GemxPipelineQueue(Path(tmp), wait_for_warm=False)
            self.assertEqual(q.workers, 2)
            for sid in ("A", "B", "C", "D"):
                q.enqueue(Path(tmp) / sid)
            deadline = time.time() + 3
            while len(started) < 2 and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(sorted(started), ["A", "B"])           # two run side by side
            self.assertEqual(q.jobs_ahead("A"), (0, True))
            self.assertEqual(q.jobs_ahead("C"), (0, False))
            self.assertEqual(q.jobs_ahead("D"), (1, False))
            self.assertIsNone(q.jobs_ahead("Z"))
            gate.set()
            deadline = time.time() + 5
            while (q._pending or q._running or len(started) < 4) and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(started), 4)

    def test_safe_extract_rejects_traversal(self):
        import io
        import sys
        import tarfile

        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import gemx_pipeline

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"x"
            info = tarfile.TarInfo("../evil.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                gemx_pipeline._safe_extract(buf.getvalue(), Path(tmp) / "out")
