"""Admin dashboard, password reset/change, sign-in limits, visits, feedback and stored coach questions."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from motion_agent.app import activity, admin, chat_server, questions, rate_limit, site_traffic
from motion_agent.app.accounts import (
    change_password,
    create_password_reset,
    delete_account,
    login,
    register,
    reset_password,
    user_from_token,
)
from motion_agent.app.config import Settings
from motion_agent.app.library import add_capture


def _settings(data: Path) -> Settings:
    return Settings(
        data_dir=data, host="127.0.0.1", port=1, backend="mock", prompt_password=None,
        max_upload_bytes=1024 * 1024, invite_daily_limit=3,
    )


class PasswordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.product = Path(self.tmp.name)
        self.user = register(self.product, "golfer@example.com", "first-pass")

    def tearDown(self):
        self.tmp.cleanup()

    def test_reset_link_only_for_existing_accounts(self):
        self.assertIsNone(create_password_reset(self.product, "nobody@example.com"))
        self.assertTrue(create_password_reset(self.product, "Golfer@Example.com "))

    def test_reset_works_once_and_signs_out_other_devices(self):
        token = create_password_reset(self.product, "golfer@example.com")
        result = reset_password(self.product, token, "second-pass")
        self.assertIsNone(user_from_token(self.product, self.user["token"]))
        self.assertTrue(user_from_token(self.product, result["token"]))
        self.assertTrue(login(self.product, "golfer@example.com", "second-pass")["ok"])
        with self.assertRaises(ValueError):
            login(self.product, "golfer@example.com", "first-pass")
        with self.assertRaises(ValueError):
            reset_password(self.product, token, "third-pass1")

    def test_reset_token_is_not_stored_in_clear(self):
        token = create_password_reset(self.product, "golfer@example.com")
        self.assertNotIn(token, (self.product / "password_resets.json").read_text())

    def test_expired_reset_link_is_refused(self):
        token = create_password_reset(self.product, "golfer@example.com", ttl=timedelta(seconds=-1))
        with self.assertRaises(ValueError):
            reset_password(self.product, token, "second-pass")

    def test_short_new_password_is_refused(self):
        token = create_password_reset(self.product, "golfer@example.com")
        with self.assertRaises(ValueError):
            reset_password(self.product, token, "short")
        # A refused attempt does not use up the link.
        self.assertTrue(reset_password(self.product, token, "long-enough")["ok"])

    def test_change_password_needs_the_current_one(self):
        uid = self.user["user"]["user_id"]
        with self.assertRaises(ValueError):
            change_password(self.product, uid, "wrong-pass", "second-pass")
        result = change_password(self.product, uid, "first-pass", "second-pass")
        self.assertIsNone(user_from_token(self.product, self.user["token"]))
        self.assertTrue(user_from_token(self.product, result["token"]))

    def test_delete_account_removes_visits_questions_and_reset_links(self):
        uid = self.user["user"]["user_id"]
        activity.touch(self.product, uid)
        questions.record(self.product, uid, "S1", "Why is my hip early?")
        create_password_reset(self.product, "golfer@example.com")
        delete_account(self.product, uid)
        self.assertEqual(activity.visits_for(self.product, uid), [])
        self.assertEqual(questions.rows(self.product, user_id=uid), [])
        self.assertEqual(json.loads((self.product / "password_resets.json").read_text()), {})


class ActivityTests(unittest.TestCase):
    def test_visits_merge_until_a_30_minute_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp)
            t0 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
            activity.touch(product, "u1", now=t0)
            activity.touch(product, "u1", now=t0 + timedelta(seconds=10))  # throttled, no write
            activity.touch(product, "u1", now=t0 + timedelta(minutes=5))
            activity.touch(product, "u1", now=t0 + timedelta(minutes=50))  # 45 min later: new visit
            visits = activity.visits_for(product, "u1")
            self.assertEqual(len(visits), 2)
            self.assertEqual(visits[1]["seconds"], 300)
            self.assertEqual(visits[0]["seconds"], 0)

    def test_prune_drops_old_visits_and_deleted_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp)
            old = datetime(2024, 1, 1, tzinfo=timezone.utc)
            now = datetime(2026, 10, 26, tzinfo=timezone.utc)
            activity.touch(product, "u1", now=old)
            activity.touch(product, "u1", now=now)
            activity.touch(product, "gone", now=now)
            removed = activity.prune(product, older_than=now - timedelta(days=30), keep_users={"u1"})
            self.assertEqual(removed, 2)
            self.assertEqual(len(activity.visits_for(product, "u1")), 1)
            self.assertEqual(activity.visits_for(product, "gone"), [])


class QuestionTests(unittest.TestCase):
    def test_scrub_by_swing_and_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp)
            questions.record(product, "u1", "S1", "a")
            questions.record(product, "u1", "S2", "b")
            self.assertEqual(questions.scrub(product, session_ids=["S1"]), 1)
            self.assertEqual([q["question"] for q in questions.rows(product)], ["b"])
            future = datetime.now(timezone.utc) + timedelta(days=1)
            self.assertEqual(questions.scrub(product, older_than=future), 1)

    def test_only_accounts_on_the_current_policy_are_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            product = Path(tmp)
            new = register(product, "new@example.com", "long-pass")["user"]["user_id"]
            old = register(product, "old@example.com", "long-pass")["user"]["user_id"]
            accounts = json.loads((product / "accounts.json").read_text())
            accounts["old@example.com"]["terms_version"] = "2026-10-24"
            (product / "accounts.json").write_text(json.dumps(accounts))
            self.assertTrue(questions.may_store(product, new))
            self.assertFalse(questions.may_store(product, old))


def _serve() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
    server.operator = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class AdminHttpTests(unittest.TestCase):
    ENV = {
        "CEREBEL_ADMIN_EMAILS": "boss@example.com",
        "CEREBEL_ADMIN_PASSWORD": "admin-secret-123",
        "CEREBEL_PUBLIC_URL": "https://app.example.com",
        "CEREBEL_DEMO_SESSION_ID": "",
        "CEREBEL_SMTP_HOST": "",
        "STRIPE_SECRET_KEY": "",
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.product = self.data / "product"
        self.settings = _settings(self.data)
        self.patches = [
            mock.patch.dict(os.environ, self.ENV),
            mock.patch.object(chat_server, "SETTINGS", self.settings),
            mock.patch.object(chat_server, "DATA_DIR", self.data),
            mock.patch.object(chat_server, "OPERATOR_PORT", 8093),
        ]
        for p in self.patches:
            p.start()
        rate_limit.clear_all()
        admin._SESSIONS.clear()  # noqa: SLF001
        self.server = _serve()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def call(self, path, body=None, headers=None, method=None):
        url = f"http://127.0.0.1:{self.server.server_address[1]}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, raw, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def json_call(self, path, body=None, headers=None):
        code, raw, _ = self.call(path, body, headers)
        try:
            return code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return code, {}

    def signup(self, email, password="long-pass-1"):
        code, data = self.json_call("/api/auth/register", {"email": email, "password": password, "accept_terms": True})
        self.assertEqual(code, 200, data)
        return data

    def admin_headers(self):
        user = self.signup("boss@example.com")
        auth = {"Authorization": "Bearer " + user["token"]}
        code, data = self.json_call("/api/admin/session", {"password": "admin-secret-123"}, auth)
        self.assertEqual(code, 200, data)
        return {**auth, "X-Admin-Token": data["admin_token"]}

    # -------------------------------------------------------------- gate

    def test_dashboard_does_not_exist_without_configuration(self):
        with mock.patch.dict(os.environ, {"CEREBEL_ADMIN_PASSWORD": ""}):
            self.assertEqual(self.call("/admin")[0], 404)
            self.assertEqual(self.json_call("/api/admin/overview")[0], 404)
            self.assertEqual(self.json_call("/api/admin/session", {"password": "x"})[0], 404)

    def test_admin_page_headers(self):
        code, raw, headers = self.call("/admin")
        self.assertEqual(code, 200)
        self.assertIn(b"/static/admin.js", raw)
        self.assertIn("frame-ancestors 'none'", headers.get("Content-Security-Policy", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_needs_admin_email_and_admin_password(self):
        user = self.signup("player@example.com")
        code, data = self.json_call("/api/admin/session", {"password": "admin-secret-123"})
        self.assertEqual(code, 401)
        code, data = self.json_call("/api/admin/session", {"password": "admin-secret-123"},
                                    {"Authorization": "Bearer " + user["token"]})
        self.assertEqual(code, 403)
        boss = self.signup("boss@example.com")
        auth = {"Authorization": "Bearer " + boss["token"]}
        self.assertEqual(self.json_call("/api/admin/session", {"password": "nope"}, auth)[0], 401)
        self.assertEqual(self.json_call("/api/admin/overview", None, auth)[0], 401)

    def test_admin_password_guesses_are_limited(self):
        boss = self.signup("boss@example.com")
        auth = {"Authorization": "Bearer " + boss["token"]}
        for _ in range(5):
            self.assertEqual(self.json_call("/api/admin/session", {"password": "guess"}, auth)[0], 401)
        self.assertEqual(self.json_call("/api/admin/session", {"password": "admin-secret-123"}, auth)[0], 429)

    def test_admin_session_needs_the_account_sign_in_too(self):
        headers = self.admin_headers()
        self.assertEqual(self.json_call("/api/admin/overview", None, {"X-Admin-Token": headers["X-Admin-Token"]})[0], 401)
        self.json_call("/api/auth/logout", {}, headers)
        code, data = self.json_call("/api/admin/overview", None, headers)
        self.assertEqual(code, 401)
        self.assertTrue(data.get("relogin"))

    def test_logout_ends_admin_session(self):
        headers = self.admin_headers()
        self.assertEqual(self.json_call("/api/admin/logout", {}, headers)[0], 200)
        self.assertEqual(self.json_call("/api/admin/users", None, headers)[0], 401)

    # -------------------------------------------------------------- data

    def test_users_detail_feedback_and_video(self):
        player = self.signup("player@example.com")
        uid = player["user"]["user_id"]
        auth = {"Authorization": "Bearer " + player["token"]}
        self.assertEqual(self.json_call("/api/auth/login", {"email": "player@example.com", "password": "long-pass-1"})[0], 200)
        sid = "SWING-1"
        upload = self.data / "sessions" / sid / "upload"
        upload.mkdir(parents=True)
        (upload / "source_video.mp4").write_bytes(b"\0" * 4096)
        add_capture(self.product, user_id=uid, session_id=sid, filename="range.mp4", status="ready")
        self.assertEqual(self.json_call("/api/feedback", {"text": "Hip note was spot on", "session_id": sid, "rating": "yes"}, auth)[0], 200)
        self.assertEqual(self.json_call("/api/feedback", {"text": "   "}, auth)[0], 400)
        self.assertEqual(self.json_call("/api/feedback", {"text": "anon"})[0], 401)
        self.assertEqual(self.json_call("/api/activity/ping", {}, auth)[0], 200)
        questions.record(self.product, uid, sid, "How do I stop early extension?")

        headers = self.admin_headers()
        code, overview = self.json_call("/api/admin/overview", None, headers)
        self.assertEqual(code, 200)
        self.assertEqual(overview["accounts"]["total"], 2)
        self.assertEqual(overview["feedback"]["written"], 1)

        code, data = self.json_call("/api/admin/users", None, headers)
        row = next(u for u in data["users"] if u["email"] == "player@example.com")
        self.assertEqual(row["logins"], 1)
        self.assertEqual(row["uploads"], 1)
        self.assertGreaterEqual(row["visits"], 1)
        self.assertNotIn("password_hash", json.dumps(data))

        code, detail = self.json_call(f"/api/admin/user?user_id={uid}", None, headers)
        self.assertEqual(code, 200)
        self.assertNotIn("salt", detail["account"])
        self.assertEqual(detail["feedback"][0]["text"], "Hip note was spot on")
        self.assertEqual(detail["questions"][0]["question"], "How do I stop early extension?")
        self.assertEqual({e["kind"] for e in detail["logins"]}, {"signup", "login"})
        self.assertTrue(detail["captures"][0]["has_video"])

        code, payments = self.json_call(f"/api/admin/payments?user_id={uid}", None, headers)
        self.assertEqual(code, 200)
        self.assertEqual(payments["invoices"], [])

        code, video = self.json_call(f"/api/admin/video?session_id={sid}", None, headers)
        self.assertEqual(code, 200, video)
        url = video["files"][0]["url"]
        self.assertIn("mt=", url)
        status, body, _ = self.call(url)
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 4096)
        code, audit = self.json_call("/api/admin/audit", None, headers)
        self.assertIn(("video_view", sid), [(r["action"], r.get("session_id")) for r in audit["rows"]])

        code, fb = self.json_call("/api/admin/feedback", None, headers)
        self.assertEqual(fb["feedback"][0]["email"], "player@example.com")

    def test_admin_reset_link_lets_the_user_choose_a_password(self):
        player = self.signup("player@example.com")
        headers = self.admin_headers()
        code, data = self.json_call("/api/admin/reset-link", {"user_id": player["user"]["user_id"]}, headers)
        self.assertEqual(code, 200)
        self.assertTrue(data["link"].startswith("https://app.example.com/#reset="))
        token = data["link"].split("#reset=", 1)[1]
        code, reset = self.json_call("/api/auth/reset", {"token": token, "password": "brand-new-pass"})
        self.assertEqual(code, 200)
        self.assertIsNone(user_from_token(self.product, player["token"]))
        self.assertEqual(self.json_call("/api/auth/login", {"email": "player@example.com", "password": "brand-new-pass"})[0], 200)

    # -------------------------------------------------------------- sign-in

    def test_failed_sign_ins_are_limited(self):
        self.signup("player@example.com")
        for _ in range(10):
            self.assertEqual(self.json_call("/api/auth/login", {"email": "player@example.com", "password": "wrong"})[0], 401)
        code, data = self.json_call("/api/auth/login", {"email": "player@example.com", "password": "long-pass-1"})
        self.assertEqual(code, 429)
        self.assertIn("Too many", data["message"])

    def test_forgot_password_without_email_service(self):
        code, data = self.json_call("/api/auth/forgot", {"email": "player@example.com"})
        self.assertEqual(code, 200)
        self.assertFalse(data["emailed"])

    def test_forgot_password_answers_the_same_for_unknown_emails(self):
        self.signup("player@example.com")
        with mock.patch("motion_agent.app.notify.enabled", return_value=True), \
                mock.patch("motion_agent.app.notify.send_password_reset", return_value=True) as send:
            known = self.json_call("/api/auth/forgot", {"email": "player@example.com"})
            unknown = self.json_call("/api/auth/forgot", {"email": "ghost@example.com"})
        self.assertEqual(known, unknown)
        self.assertEqual(send.call_count, 1)
        self.assertIn("#reset=", send.call_args[0][1])

    def test_change_password_over_http(self):
        player = self.signup("player@example.com")
        auth = {"Authorization": "Bearer " + player["token"]}
        code, data = self.json_call("/api/account/password", {"current_password": "bad", "new_password": "another-pass"}, auth)
        self.assertEqual(code, 400)
        code, data = self.json_call("/api/account/password", {"current_password": "long-pass-1", "new_password": "another-pass"}, auth)
        self.assertEqual(code, 200)
        self.assertEqual(self.json_call("/api/activity/ping", {}, auth)[0], 401)
        self.assertEqual(self.json_call("/api/activity/ping", {}, {"Authorization": "Bearer " + data["token"]})[0], 200)

    def test_deleting_a_swing_removes_its_questions(self):
        player = self.signup("player@example.com")
        uid = player["user"]["user_id"]
        add_capture(self.product, user_id=uid, session_id="S9", filename="a.mp4", status="ready")
        questions.record(self.product, uid, "S9", "question about S9")
        code, _ = self.json_call("/api/library/delete", {"session_id": "S9"}, {"Authorization": "Bearer " + player["token"]})
        self.assertEqual(code, 200)
        self.assertEqual(questions.rows(self.product, user_id=uid), [])

    def test_privacy_page_shows_question_retention(self):
        code, raw, _ = self.call("/privacy")
        self.assertEqual(code, 200)
        self.assertIn(b"90 days", raw)
        self.assertNotIn(b"{{QUESTION_RETENTION_DAYS}}", raw)


if __name__ == "__main__":
    unittest.main()


BROWSER = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Mobile/15E148 Safari/604.1", "CF-IPCountry": "US"}


class SiteTrafficTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.product = Path(self.tmp.name)
        rate_limit.clear_all()
        site_traffic.clear_presence()

    def tearDown(self):
        self.tmp.cleanup()

    def ping(self, event="page_view", visitor="visitor-1", headers=None, ip="1.1.1.1", user=None, now=None, **fields):
        body = {"eventType": event, "visitor": visitor, **fields}
        return site_traffic.handle_ping(self.product, body, headers or BROWSER, ip, user, now=now)

    def stored(self):
        return [json.loads(x) for x in (self.product / "site_events.jsonl").read_text().splitlines()]

    def test_query_string_ip_and_user_agent_are_not_stored(self):
        self.ping(path="/?reset=SECRET-TOKEN#x", referrer="https://news.example.com/a?b=SECRET-REF")
        raw = (self.product / "site_events.jsonl").read_text()
        row = self.stored()[0]
        self.assertEqual((row["path"], row["ref"], row["country"], row["device"]), ("/", "news.example.com", "US", "mobile"))
        for leak in ("SECRET", "1.1.1.1", "iPhone"):
            self.assertNotIn(leak, raw)

    def test_heartbeat_marks_online_without_writing(self):
        self.assertEqual(self.ping("heartbeat")[0], 200)
        self.assertFalse((self.product / "site_events.jsonl").exists())
        self.assertEqual([r["visitor"] for r in site_traffic.online()], ["visitor-1"])

    def test_online_expires_and_keeps_arrival_source(self):
        start = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        self.ping(referrer="https://news.example.com/", now=start)
        self.ping("heartbeat", path="/next", referrer="https://cerebel.tech/", now=start + timedelta(seconds=60))
        row = site_traffic.online(now=start + timedelta(seconds=61))[0]
        self.assertEqual((row["ref"], row["path"], row["seconds"]), ("news.example.com", "/next", 60))
        self.assertEqual(site_traffic.online(now=start + timedelta(seconds=151)), [])

    def test_bad_events_bots_and_floods_are_refused(self):
        self.assertEqual(self.ping("drop_table")[0], 400)
        self.assertEqual(self.ping(visitor="x")[0], 400)
        self.assertEqual(self.ping(headers={"User-Agent": "Googlebot/2.1"})[0], 200)
        self.assertFalse((self.product / "site_events.jsonl").exists())
        for _ in range(rate_limit.SITE_PING_PER_IP.limit):
            rate_limit.SITE_PING_PER_IP.hit("9.9.9.9")
        self.assertEqual(self.ping(ip="9.9.9.9")[0], 429)

    def test_traffic_counts_unique_visitors_per_day_and_site(self):
        now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        self.ping(now=now, site="main", utmSource="reddit")
        self.ping(now=now, site="main")  # same visitor, same day
        self.ping(now=now, visitor="visitor-2", site="playground")
        self.ping(now=now - timedelta(days=1), visitor="visitor-3", site="main")
        self.ping(now=now - timedelta(days=20), visitor="visitor-4", site="main")  # outside the window
        out = site_traffic.traffic(self.product, 7, now=now)
        self.assertEqual(len(out["labels"]), 7)
        self.assertEqual(out["visitors_main"][-2:], [1, 1])
        self.assertEqual(out["visitors_playground"][-1], 1)
        self.assertEqual(out["total_visitors"], 3)
        self.assertEqual(out["total_page_views"], 4)
        self.assertIn({"name": "reddit", "count": 1}, out["sources"])

    def test_deleting_an_account_removes_its_rows(self):
        user = register(self.product, "golfer@example.com", "first-pass")
        self.ping(user={"user_id": user["user"]["user_id"]})
        self.ping(visitor="visitor-2")
        delete_account(self.product, user["user"]["user_id"])
        self.assertEqual([r["visitor"] for r in self.stored()], ["visitor-2"])
        self.assertEqual([r["visitor"] for r in site_traffic.online()], ["visitor-2"])

    def test_prune_removes_old_rows_only(self):
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        self.ping(now=now - timedelta(days=91), visitor="visitor-old")
        self.ping(now=now, visitor="visitor-new")
        self.assertEqual(site_traffic.prune(self.product, older_than=now - timedelta(days=90), dry_run=True), 1)
        self.assertEqual(len(self.stored()), 2)
        self.assertEqual(site_traffic.prune(self.product, older_than=now - timedelta(days=90)), 1)
        self.assertEqual([r["visitor"] for r in self.stored()], ["visitor-new"])

    def test_backer_signup_dedupes_and_keeps_first_date(self):
        body = {"email": " Fan@Example.com ", "name": "Fan", "use": "Research", "referrer": "https://news.example.com/"}
        self.assertEqual(site_traffic.handle_backer(self.product, body, BROWSER, "1.1.1.1"), (200, {"ok": True}))
        first = site_traffic.backers(self.product)[0]
        self.assertEqual(site_traffic.handle_backer(self.product, {"email": "fan@example.com"}, BROWSER, "1.1.1.1"), (200, {"ok": True}))
        rows = site_traffic.backers(self.product)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["email"], rows[0]["name"], rows[0]["created_at"], rows[0]["signups"]),
                         ("fan@example.com", "Fan", first["created_at"], 2))

    def test_backer_bad_email_and_flood_refused(self):
        self.assertEqual(site_traffic.handle_backer(self.product, {"email": "nope"}, BROWSER, "1.1.1.1")[0], 400)
        for i in range(rate_limit.BACKER_PER_IP.limit):
            self.assertEqual(site_traffic.handle_backer(self.product, {"email": f"a{i}@example.com"}, BROWSER, "2.2.2.2")[0], 200)
        self.assertEqual(site_traffic.handle_backer(self.product, {"email": "z@example.com"}, BROWSER, "2.2.2.2")[0], 429)

    def test_corrupt_backer_list_is_not_overwritten(self):
        path = self.product / "backers.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            site_traffic.handle_backer(self.product, {"email": "a@example.com"}, BROWSER, "1.1.1.1")
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


class SupabaseBackerTests(unittest.TestCase):
    ENV = {"CEREBEL_SUPABASE_URL": "https://x.example.test", "CEREBEL_SUPABASE_KEY": "k", "CEREBEL_BACKERS_ADMIN_TOKEN": "t"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.product = Path(self.tmp.name)
        rate_limit.clear_all()
        site_traffic._forget_remote_cache()  # noqa: SLF001
        patcher = mock.patch.dict(os.environ, self.ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(site_traffic._forget_remote_cache)  # noqa: SLF001

    def test_list_is_read_with_the_token_and_cached(self):
        rows = [{"id": "a", "email": "fan@example.com", "use_case": "Research", "created_at": "2026-11-02T00:00:00+00:00"}]
        with mock.patch.object(site_traffic, "_rpc", return_value=rows) as rpc:
            first = site_traffic.backers(self.product)
            site_traffic.backers(self.product)
        rpc.assert_called_once_with("admin_list_backers", {"token": "t"})
        self.assertEqual((first[0]["email"], first[0]["use"]), ("fan@example.com", "Research"))
        self.assertIsNone(site_traffic.backer_store_error())

    def test_failed_read_keeps_last_list_and_reports_it(self):
        rows = [{"id": "a", "email": "fan@example.com"}]
        with mock.patch.object(site_traffic, "_rpc", return_value=rows):
            site_traffic.backers(self.product)
        site_traffic._remote["at"] = None  # noqa: SLF001
        with mock.patch.object(site_traffic, "_rpc", side_effect=RuntimeError("down")):
            self.assertEqual(site_traffic.backers(self.product)[0]["email"], "fan@example.com")
        self.assertTrue(site_traffic.backer_store_error())

    def test_signup_through_server_goes_to_supabase_not_the_file(self):
        with mock.patch.object(site_traffic, "_rpc") as rpc:
            code, _ = site_traffic.handle_backer(self.product, {"email": "fan@example.com"}, BROWSER, "1.1.1.1")
        self.assertEqual(code, 200)
        self.assertEqual(rpc.call_args[0][0], "join_backer_list")
        self.assertFalse((self.product / "backers.json").exists())

    def test_remove_uses_token_and_refreshes(self):
        with mock.patch.object(site_traffic, "_rpc", return_value="fan@example.com") as rpc:
            self.assertEqual(site_traffic.remove_backer(self.product, "a"), "fan@example.com")
        rpc.assert_called_once_with("admin_remove_backer", {"token": "t", "backer_id": "a"})
        with mock.patch.object(site_traffic, "_rpc", return_value=None):
            self.assertIsNone(site_traffic.remove_backer(self.product, "gone"))


class SiteTrafficHttpTests(AdminHttpTests):
    """Everything together over HTTP, on the same server setup as the admin tests."""

    def setUp(self):
        super().setUp()
        site_traffic.clear_presence()

    def post_public(self, path, body, headers=None):
        code, raw, _ = self.call(path, body, {**BROWSER, **(headers or {})})
        return code, json.loads(raw or b"{}")

    def test_new_admin_routes_need_an_admin_session(self):
        for path in ("/api/admin/live", "/api/admin/traffic", "/api/admin/backers"):
            self.assertEqual(self.json_call(path)[0], 401, path)
        self.assertEqual(self.json_call("/api/admin/backers/remove", {"id": "abc"})[0], 401)
        user = self.signup("stranger@example.com")
        auth = {"Authorization": "Bearer " + user["token"]}
        self.assertEqual(self.json_call("/api/admin/backers", headers=auth)[0], 401)

    def test_visitor_and_backer_reach_the_dashboard(self):
        admin_h = self.admin_headers()
        self.assertEqual(self.post_public("/api/site/ping", {"eventType": "page_view", "visitor": "visitor-1", "site": "main", "path": "/"})[0], 200)
        self.assertEqual(self.post_public("/api/site/backers", {"email": "fan@example.com", "visitor": "visitor-1"})[0], 200)
        code, live = self.json_call("/api/admin/live", headers=admin_h)
        self.assertEqual(code, 200)
        self.assertEqual([r["visitor"] for r in live["online"]], ["visitor-1"])
        self.assertEqual((live["today"]["visitors"], live["today"]["backers"], live["backers_total"]), (1, 1, 1))
        self.assertIn("backer", [e["type"] for e in live["feed"]])
        code, traffic = self.json_call("/api/admin/traffic?days=7", headers=admin_h)
        self.assertEqual((code, traffic["days"], traffic["total_visitors"], traffic["backers_running"][-1]), (200, 7, 1, 1))
        code, listing = self.json_call("/api/admin/backers", headers=admin_h)
        self.assertEqual(listing["backers"][0]["email"], "fan@example.com")
        code, _ = self.json_call("/api/admin/backers/remove", {"id": listing["backers"][0]["id"]}, admin_h)
        self.assertEqual(code, 200)
        self.assertEqual(self.json_call("/api/admin/backers", headers=admin_h)[1]["backers"], [])
        audit = (self.product / "admin_audit.jsonl").read_text()
        self.assertIn("backer_removed", audit)
        self.assertNotIn("fan@example.com", audit)

    def test_signed_in_visitor_shows_email_but_admin_is_not_counted(self):
        admin_h = self.admin_headers()
        user = self.signup("golfer@example.com")
        self.post_public("/api/site/ping", {"eventType": "page_view", "visitor": "visitor-2", "site": "playground"}, {"Authorization": "Bearer " + user["token"]})
        self.post_public("/api/site/ping", {"eventType": "page_view", "visitor": "visitor-3"}, {"Authorization": admin_h["Authorization"]})
        online = self.json_call("/api/admin/live", headers=admin_h)[1]["online"]
        self.assertEqual([(r["visitor"], r["email"]) for r in online], [("visitor-2", "golfer@example.com")])

    def test_oversized_body_refused(self):
        code, _ = self.post_public("/api/site/backers", {"email": "a@example.com", "name": "x" * 5000})
        self.assertEqual(code, 413)
