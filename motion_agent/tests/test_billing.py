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
