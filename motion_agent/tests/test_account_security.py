"""Account, subscription and deletion safety (production readiness sprint, P0).

- Deleting an account is refused while Stripe can still charge it (past_due / unpaid too).
- A second checkout is refused while a subscription has a payment problem.
- Stripe webhooks arriving out of order cannot revive a canceled subscription or roll a
  newer status back, and webhooks for a deleted account do not recreate its billing row.
- A capture deleted while the GPU is still working does not come back when the pipeline ends,
  and the daily retention job removes any such leftovers.
- Sign-in and sign-up are rate limited; request bodies on the JSON endpoints are capped.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from motion_agent.app import billing, rate_limit
from motion_agent.app.accounts import profile, register, user_from_token
from motion_agent.app.config import Settings
from motion_agent.app.library import add_capture, all_captures, delete_capture

SECRET = "whsec_test_secret"


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


def _sign(payload: bytes) -> str:
    ts = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = _settings(self.root)
        self.product = self.settings.product_dir
        self.env = mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": SECRET})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _send(self, event: dict) -> dict:
        raw = json.dumps(event).encode()
        return billing.handle_webhook(self.product, raw, _sign(raw))

    def _sub_event(self, kind: str, status: str, created: int, *, user: str = "u1", sub: str = "sub_1",
                   evt: str | None = None, cancel_at_period_end: bool = False) -> dict:
        return {
            "id": evt or f"evt_{kind}_{status}_{created}",
            "type": kind,
            "created": created,
            "data": {"object": {
                "id": sub, "customer": "cus_1", "status": status, "metadata": {"user_id": user},
                "cancel_at_period_end": cancel_at_period_end,
                "items": {"data": [{"current_period_end": int(time.time()) + 30 * 86400}]},
            }},
        }

    def _set_row(self, user_id: str, **row) -> None:
        data = billing._read(self.product)
        data["users"][user_id] = row
        billing._write(self.product, data)


class AccountDeletionBillingTests(_Tmp):
    def _user(self) -> dict:
        created = register(self.product, "payer@example.com", "longenough")
        return user_from_token(self.product, created["token"])

    def test_past_due_subscription_blocks_deletion(self):
        from motion_agent.app.product_api import remove_account

        user = self._user()
        for status in ("past_due", "unpaid", "active", "trialing"):
            with self.subTest(status=status):
                self._set_row(user["user_id"], status=status, customer="cus_1", subscription="sub_1")
                code, payload = remove_account(self.settings, user)
                self.assertEqual(code, 409)
                self.assertTrue(payload["subscription_active"])
                self.assertTrue(profile(self.product, user["user_id"]))

    def test_cancel_at_period_end_or_canceled_allows_deletion(self):
        from motion_agent.app.product_api import remove_account

        for row in ({"status": "active", "cancel_at_period_end": True}, {"status": "canceled"},
                    {"status": "incomplete_expired"}):
            with self.subTest(row=row):
                user = register(self.product, f"u{time.monotonic_ns()}@example.com", "longenough")["user"]
                self._set_row(user["user_id"], customer="cus_1", subscription="sub_1", **row)
                code, _ = remove_account(self.settings, user)
                self.assertEqual(code, 200)
                self.assertEqual(profile(self.product, user["user_id"]), {})
                self.assertEqual(billing.subscription_for(self.product, user["user_id"]), {})

    def test_webhooks_for_a_deleted_account_do_not_recreate_its_row(self):
        from motion_agent.app.product_api import remove_account

        user = self._user()
        uid = user["user_id"]
        self._set_row(uid, status="active", cancel_at_period_end=True, customer="cus_1", subscription="sub_1")
        self.assertEqual(remove_account(self.settings, user)[0], 200)
        # Stripe ends the subscription at period end and tells us about it.
        result = self._send(self._sub_event("customer.subscription.deleted", "canceled", int(time.time()), user=uid))
        self.assertIsNone(result["user_id"])
        self.assertNotIn(uid, billing._read(self.product)["users"])

    def test_still_billing_statuses(self):
        for status, expected in (("active", True), ("trialing", True), ("past_due", True), ("unpaid", True),
                                 ("paused", True), ("canceled", False), ("incomplete", False),
                                 ("incomplete_expired", False), (None, False)):
            with self.subTest(status=status):
                self._set_row("u9", status=status)
                self.assertEqual(billing.still_billing(self.product, "u9"), expected)
        self._set_row("u9", status="past_due", cancel_at_period_end=True)
        self.assertFalse(billing.still_billing(self.product, "u9"))


class CheckoutGuardTests(_Tmp):
    def test_no_second_subscription_while_past_due(self):
        self._set_row("u1", status="past_due", customer="cus_1", subscription="sub_1")
        env = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PRICE_ANNUAL": "price_a"}
        with mock.patch.dict(os.environ, env), mock.patch.object(billing, "_stripe_request") as stripe:
            with self.assertRaises(billing.BillingError) as ctx:
                billing.create_checkout(self.product, {"user_id": "u1", "email": "a@b.co"}, "annual")
        self.assertEqual(ctx.exception.status, 409)
        stripe.assert_not_called()

    def test_checkout_allowed_after_cancel(self):
        self._set_row("u1", status="canceled", customer="cus_1", subscription="sub_1")
        env = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PRICE_ANNUAL": "price_a"}
        with mock.patch.dict(os.environ, env), mock.patch.object(
            billing, "_stripe_request", return_value={"url": "https://checkout", "id": "cs_1"}
        ):
            self.assertTrue(billing.create_checkout(self.product, {"user_id": "u1"}, "annual")["ok"])


class WebhookOrderTests(_Tmp):
    def test_late_update_cannot_revive_a_canceled_subscription(self):
        now = int(time.time())
        self._send(self._sub_event("customer.subscription.updated", "active", now - 100))
        self._send(self._sub_event("customer.subscription.deleted", "canceled", now))
        # The "active" update from before the cancellation is delivered late.
        self._send(self._sub_event("customer.subscription.updated", "active", now - 50, evt="evt_late"))
        self.assertFalse(billing.is_pro(self.product, "u1"))
        self.assertEqual(billing.subscription_for(self.product, "u1")["status"], "canceled")

    def test_canceled_is_final_for_that_subscription(self):
        now = int(time.time())
        self._send(self._sub_event("customer.subscription.deleted", "canceled", now))
        self._send(self._sub_event("customer.subscription.updated", "active", now + 5, evt="evt_after"))
        self.assertFalse(billing.is_pro(self.product, "u1"))

    def test_stale_checkout_does_not_roll_back_a_newer_status(self):
        now = int(time.time())
        self._send(self._sub_event("customer.subscription.updated", "past_due", now))
        self._send({
            "id": "evt_checkout", "type": "checkout.session.completed", "created": now - 60,
            "data": {"object": {"mode": "subscription", "client_reference_id": "u1", "customer": "cus_1",
                                "subscription": "sub_1", "metadata": {"plan": "annual"}}},
        })
        self.assertEqual(billing.subscription_for(self.product, "u1")["status"], "past_due")

    def test_new_subscription_after_a_cancel_works(self):
        now = int(time.time())
        self._send(self._sub_event("customer.subscription.deleted", "canceled", now - 100))
        self._send({
            "id": "evt_checkout2", "type": "checkout.session.completed", "created": now,
            "data": {"object": {"mode": "subscription", "client_reference_id": "u1", "customer": "cus_1",
                                "subscription": "sub_2", "metadata": {"plan": "founding"}}},
        })
        self.assertTrue(billing.is_pro(self.product, "u1"))
        self.assertEqual(billing._read(self.product)["founding_used"], 1)

    def test_events_without_created_still_apply(self):
        event = self._sub_event("customer.subscription.updated", "active", 0)
        event.pop("created")
        self._send(event)
        self.assertTrue(billing.is_pro(self.product, "u1"))


class DeletedWhileProcessingTests(_Tmp):
    def test_pipeline_finishing_after_delete_leaves_nothing(self):
        from motion_agent.app import product_api

        created = register(self.product, "quick@example.com", "longenough")
        user = user_from_token(self.product, created["token"])
        add_capture(self.product, user_id=user["user_id"], session_id="SIDX", filename="a.mp4")
        session = self.root / "sessions" / "SIDX"

        def fake_process(session_id, name):
            # The user deletes the swing while the GPU works, then the pipeline writes results.
            self.assertEqual(product_api.remove_capture(self.settings, user, session_id)[0], 200)
            (session / "ik").mkdir(parents=True)
            (session / "ik" / "opensim-motion.json").write_text("{}", encoding="utf-8")
            product_api._write_progress(self.settings, session_id, stage="ready", percent=100)

        fake = types.ModuleType("upload_server")
        fake._process_uploaded_session = fake_process
        with mock.patch.dict(sys.modules, {"upload_server": fake}), mock.patch.object(
            product_api, "finalize_capture"
        ) as finalize, mock.patch.object(product_api, "_email_owner") as email:
            product_api._run_pipeline(self.settings, "SIDX", "a.mp4")
        self.assertFalse(session.exists())
        finalize.assert_not_called()
        email.assert_not_called()

    def test_failed_start_after_delete_does_not_recreate_the_folder(self):
        from motion_agent.app import product_api

        add_capture(self.product, user_id="u1", session_id="SIDY", filename="a.mp4")
        delete_capture(self.product, "u1", "SIDY")
        fake = types.ModuleType("upload_server")

        def boom(session_id, name):
            raise RuntimeError("worker gone")

        fake._process_uploaded_session = boom
        with mock.patch.dict(sys.modules, {"upload_server": fake}), self.assertLogs(product_api.LOG, "ERROR"):
            product_api._run_pipeline(self.settings, "SIDY", "a.mp4")
        self.assertFalse((self.root / "sessions" / "SIDY").exists())

    def test_retention_removes_files_of_deleted_captures(self):
        from motion_agent.app import retention

        gemx, neck = self.root / "gemx-out", self.root / "neck-out"
        gemx.mkdir()
        neck.mkdir()
        add_capture(self.product, user_id="u1", session_id="GONE", filename="a.mp4")
        add_capture(self.product, user_id="u1", session_id="KEEP", filename="b.mp4")
        delete_capture(self.product, "u1", "GONE")
        for sid in ("GONE", "KEEP"):
            (self.root / "sessions" / sid / "ik").mkdir(parents=True)
            (self.root / "sessions" / sid / "ik" / "pose3d.json").write_text("{}", encoding="utf-8")
            (gemx / sid).mkdir()
            (gemx / sid / "hpe_results.pt").write_bytes(b"x")
        env = {"CEREBEL_GEMX_OUTPUT_ROOT": str(gemx), "CEREBEL_NECK_OUTPUT_ROOT": str(neck)}
        with mock.patch.dict(os.environ, env):
            dry = retention.run(self.settings, dry_run=True)
            self.assertEqual(dry["deleted_capture_leftovers"], ["GONE"])
            self.assertTrue((self.root / "sessions" / "GONE").exists())
            report = retention.run(self.settings)
        self.assertEqual(report["deleted_capture_leftovers"], ["GONE"])
        self.assertIn("1 deleted-capture leftovers", retention.summary_line(report))
        self.assertFalse((self.root / "sessions" / "GONE").exists())
        self.assertFalse((gemx / "GONE").exists())
        self.assertTrue((self.root / "sessions" / "KEEP" / "ik" / "pose3d.json").is_file())
        self.assertTrue((gemx / "KEEP").exists())
        # The deleted row itself stays (it is how a deleted swing answers 410).
        self.assertTrue(any(r["session_id"] == "GONE" and r["deleted"] for r in all_captures(self.product)))


class AuthRateLimitTests(_Tmp):
    def setUp(self):
        super().setUp()
        for limiter in (rate_limit.LOGIN_FAILS_PER_EMAIL, rate_limit.LOGIN_FAILS_PER_IP, rate_limit.SIGNUPS_PER_IP):
            limiter.reset()
        self.addCleanup(rate_limit.LOGIN_FAILS_PER_EMAIL.reset)
        self.addCleanup(rate_limit.LOGIN_FAILS_PER_IP.reset)
        self.addCleanup(rate_limit.SIGNUPS_PER_IP.reset)

    def _auth(self, path: str, body: dict, ip: str = "203.0.113.5") -> tuple[int, dict]:
        from motion_agent.app.product_api import handle_auth_post

        return handle_auth_post(self.settings, path, body, {}, client_ip=ip)

    def test_password_guessing_is_cut_off_per_account(self):
        register(self.product, "target@example.com", "correct-horse")
        limit = rate_limit.LOGIN_FAILS_PER_EMAIL.limit
        for i in range(limit):
            code, _ = self._auth("/api/auth/login", {"email": "target@example.com", "password": f"guess{i}"},
                                 ip=f"198.51.100.{i}")
            self.assertEqual(code, 401)
        # Even the right password waits now, from any address, and the message says why.
        code, payload = self._auth("/api/auth/login", {"email": "TARGET@example.com", "password": "correct-horse"})
        self.assertEqual(code, 429)
        self.assertIn("15 minutes", payload["message"])

    def test_success_clears_the_account_counter(self):
        register(self.product, "me@example.com", "correct-horse")
        for _ in range(rate_limit.LOGIN_FAILS_PER_EMAIL.limit - 1):
            self._auth("/api/auth/login", {"email": "me@example.com", "password": "typo"})
        self.assertEqual(self._auth("/api/auth/login", {"email": "me@example.com", "password": "correct-horse"})[0], 200)
        self.assertEqual(self._auth("/api/auth/login", {"email": "me@example.com", "password": "typo"})[0], 401)

    def test_spraying_many_accounts_from_one_address_is_cut_off(self):
        with mock.patch.object(rate_limit.LOGIN_FAILS_PER_IP, "limit", 3):
            for i in range(3):
                self.assertEqual(self._auth("/api/auth/login", {"email": f"x{i}@example.com", "password": "p"})[0], 401)
            self.assertEqual(self._auth("/api/auth/login", {"email": "x9@example.com", "password": "p"})[0], 429)
            self.assertEqual(self._auth("/api/auth/login", {"email": "x9@example.com", "password": "p"},
                                        ip="192.0.2.1")[0], 401)

    def test_signups_per_address(self):
        with mock.patch.object(rate_limit.SIGNUPS_PER_IP, "limit", 2):
            for i in range(2):
                body = {"email": f"n{i}@example.com", "password": "longenough", "accept_terms": True}
                self.assertEqual(self._auth("/api/auth/register", body)[0], 200)
            body = {"email": "n9@example.com", "password": "longenough", "accept_terms": True}
            self.assertEqual(self._auth("/api/auth/register", body)[0], 429)
            # A refused sign-up (bad input) does not use up the allowance.
            self.assertEqual(self._auth("/api/auth/register", {"email": "bad"}, ip="192.0.2.7")[0], 400)
            self.assertEqual(self._auth("/api/auth/register", body, ip="192.0.2.7")[0], 200)

    def test_window_slides(self):
        window = rate_limit.SlidingWindow(2, 60)
        window.hit("k", now=0)
        window.hit("k", now=10)
        self.assertTrue(window.blocked("k", now=59))
        self.assertFalse(window.blocked("k", now=61))


class BodyLimitHttpTests(unittest.TestCase):
    """The JSON endpoints refuse oversized or non-numeric Content-Length before reading the body."""

    @classmethod
    def setUpClass(cls):
        from motion_agent.app import chat_server

        cls.tmp = tempfile.TemporaryDirectory()
        cls.patch = mock.patch.object(chat_server, "SETTINGS", _settings(Path(cls.tmp.name)))
        cls.patch.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.patch.stop()
        cls.tmp.cleanup()

    def _post(self, path: str, length: str) -> int:
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", length)
        conn.endheaders()  # no body: a capped endpoint must answer without waiting for one
        status = conn.getresponse().status
        conn.close()
        return status

    def test_oversized_bodies_are_refused(self):
        for path in ("/api/auth/login", "/api/auth/register", "/api/library/delete", "/api/chat"):
            with self.subTest(path=path):
                self.assertEqual(self._post(path, str(50 * 1024 * 1024)), 413)
                self.assertEqual(self._post(path, "lots"), 413)

    def test_non_object_json_is_a_400(self):
        port = self.server.server_address[1]
        for path in ("/api/auth/login", "/api/library/delete", "/api/chat"):
            with self.subTest(path=path):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"[1, 2]",
                                             headers={"Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=10)
                self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
