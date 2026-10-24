"""Video-processing consent, retention enforcement and account deletion."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from motion_agent.app import product_api, retention
from motion_agent.app.accounts import (
    has_video_consent,
    profile,
    record_video_consent,
    register,
    user_from_token,
)
from motion_agent.app.config import Settings
from motion_agent.app.consent import VIDEO_CONSENT_VERSION, video_consent_text
from motion_agent.app.library import add_capture, all_captures

GOOD_META = {"duration": 4.0, "fps": 120.0, "width": 1080, "height": 1920}


def _settings(data: Path) -> Settings:
    return Settings(
        data_dir=data,
        host="127.0.0.1",
        port=1,
        backend="mock",
        prompt_password=None,
        max_upload_bytes=10_000_000,
        invite_daily_limit=3,
    )


class _TempData(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.data = root / "data"
        self.gemx = root / "gemx-out"
        self.neck = root / "neck-out"
        self.data.mkdir()
        self.gemx.mkdir()
        self.neck.mkdir()
        self.settings = _settings(self.data)
        self.env = mock.patch.dict(
            os.environ,
            {
                "CEREBEL_GEMX_OUTPUT_ROOT": str(self.gemx),
                "CEREBEL_NECK_OUTPUT_ROOT": str(self.neck),
                "CEREBEL_DEMO_SESSION_ID": "",
            },
        )
        self.env.start()
        for key in ("CEREBEL_RAW_VIDEO_RETENTION_DAYS", "CEREBEL_INACTIVE_DELETE_DAYS"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def _user(self, email: str = "golfer@example.com", consent: bool = False) -> dict:
        created = register(self.settings.product_dir, email, "swing-fix-123")
        user = user_from_token(self.settings.product_dir, created["token"])
        if consent:
            record_video_consent(self.settings.product_dir, user["user_id"], VIDEO_CONSENT_VERSION)
        return user


class VideoConsentTests(_TempData):
    def test_upload_without_consent_is_rejected_before_anything_is_stored(self):
        user = self._user()
        code, payload = product_api.save_upload(
            self.settings, user, filename="swing.mp4", file_obj=io.BytesIO(b"x" * 4096)
        )
        self.assertEqual(code, 428)
        self.assertTrue(payload["consent_required"])
        consent = payload["video_consent"]
        self.assertTrue(consent["required"])
        self.assertEqual(consent["version"], VIDEO_CONSENT_VERSION)
        for name in ("Modal", "DigitalOcean"):
            self.assertIn(name, consent["text"])
        self.assertIn("30 days", consent["text"])
        self.assertTrue(all(p["country"] == "United States" for p in consent["processors"]))
        self.assertFalse((self.data / "sessions").exists())
        self.assertEqual(all_captures(self.settings.product_dir), [])

    def test_consent_endpoint_records_version_and_time(self):
        user = self._user()
        code, _ = product_api.handle_video_consent(self.settings, user, {"version": VIDEO_CONSENT_VERSION})
        self.assertEqual(code, 400)
        code, payload = product_api.handle_video_consent(
            self.settings, user, {"accept": True, "version": "1999-01-01"}
        )
        self.assertEqual(code, 409)
        self.assertTrue(payload["video_consent"]["required"])
        self.assertFalse(has_video_consent(self.settings.product_dir, user["user_id"]))

        code, payload = product_api.handle_video_consent(
            self.settings, user, {"accept": True, "version": VIDEO_CONSENT_VERSION}
        )
        self.assertEqual(code, 200)
        self.assertFalse(payload["video_consent"]["required"])
        prof = profile(self.settings.product_dir, user["user_id"])
        self.assertEqual(prof["video_consent_version"], VIDEO_CONSENT_VERSION)
        self.assertTrue(datetime.fromisoformat(prof["video_consent_at"]))
        self.assertEqual(prof["video_consent_retention_days"], 30)
        self.assertTrue(prof["terms_version"])  # registration consent is still separate
        lib = product_api.library_payload(self.settings, user)
        self.assertFalse(lib["video_consent"]["required"])

    def test_upload_goes_through_after_consent(self):
        user = self._user(consent=True)
        with mock.patch.object(product_api, "probe_video", return_value=GOOD_META), mock.patch.object(
            product_api, "_run_pipeline", lambda *a, **k: None
        ):
            code, payload = product_api.save_upload(
                self.settings, user, filename="swing.mp4", file_obj=io.BytesIO(b"x" * 4096)
            )
        self.assertEqual(code, 200, payload)
        self.assertTrue((self.data / "sessions" / payload["session_id"] / "upload" / "source_video.mp4").is_file())

    def test_longer_retention_needs_fresh_consent(self):
        user = self._user(consent=True)
        self.assertTrue(has_video_consent(self.settings.product_dir, user["user_id"]))
        with mock.patch.dict(os.environ, {"CEREBEL_RAW_VIDEO_RETENTION_DAYS": "14"}):
            self.assertTrue(has_video_consent(self.settings.product_dir, user["user_id"]))
        with mock.patch.dict(os.environ, {"CEREBEL_RAW_VIDEO_RETENTION_DAYS": "90"}):
            self.assertFalse(has_video_consent(self.settings.product_dir, user["user_id"]))
            self.assertIn("90 days", video_consent_text())

    def test_unknown_account_has_no_consent(self):
        code, _ = product_api.save_upload(
            self.settings, {"user_id": "ghost", "email": "g@x.co"}, filename="s.mp4", file_obj=io.BytesIO(b"x" * 4096)
        )
        self.assertEqual(code, 428)


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * 86400
    for p in [path, *path.rglob("*")] if path.is_dir() else [path]:
        os.utime(p, (stamp, stamp))


class RetentionTests(_TempData):
    def _session(self, sid: str, *, processed_days_ago: float, owner: str | None) -> Path:
        s = self.data / "sessions" / sid
        for rel, body in {
            "upload/source_video.mp4": b"v" * 2048,
            "jpegs/000000.jpg": b"j" * 512,
            "ik/session.mp4": b"m" * 1024,
            "ik/poster.jpg": b"p" * 256,
            "ik/opensim-motion.json": b"{}",
            "ik/pose3d.json": b"{}",
            "upload_progress.json": b'{"stage": "ready"}',
        }.items():
            (s / rel).parent.mkdir(parents=True, exist_ok=True)
            (s / rel).write_bytes(body)
        g = self.gemx / sid
        (g / "preprocess").mkdir(parents=True)
        (g / "hpe_results.pt").write_bytes(b"params")
        (g / "preprocess" / "0_input_video.mp4").write_bytes(b"v" * 1024)
        (g / "preprocess" / "vitpose.pt").write_bytes(b"kp")
        _age(s, processed_days_ago)
        _age(g, processed_days_ago)
        if owner:
            add_capture(self.settings.product_dir, user_id=owner, session_id=sid, filename="a.mp4")
            self._set_created(sid, processed_days_ago)
        return s

    def _set_created(self, sid: str, days_ago: float) -> None:
        path = self.settings.product_dir / "library.json"
        rows = json.loads(path.read_text())
        for row in rows:
            if row["session_id"] == sid:
                row["created_at"] = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        path.write_text(json.dumps(rows))

    def _make_inactive(self, user_id: str, days: float) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        accounts = self.settings.product_dir / "accounts.json"
        data = json.loads(accounts.read_text())
        for row in data.values():
            if row["user_id"] == user_id:
                for key in ("created_at", "terms_accepted_at", "last_active_at", "video_consent_at", "last_login_at"):
                    if row.get(key):
                        row[key] = old
        accounts.write_text(json.dumps(data))
        tokens = self.settings.product_dir / "tokens.json"
        tdata = json.loads(tokens.read_text())
        for row in tdata.values():
            if row["user_id"] == user_id:
                row["created_at"] = old
        tokens.write_text(json.dumps(tdata))

    def _fixture(self):
        active = self._user("active@example.com", consent=True)
        idle = self._user("idle@example.com", consent=True)
        old = self._session("OLD1", processed_days_ago=40, owner=active["user_id"])
        new = self._session("NEW1", processed_days_ago=2, owner=active["user_id"])
        idle_s = self._session("IDLE1", processed_days_ago=800, owner=idle["user_id"])
        self._make_inactive(idle["user_id"], 800)
        product_api.record_event(self.settings, "upload_saved", user_id=idle["user_id"], session_id="IDLE1")
        product_api.record_event(self.settings, "upload_saved", user_id=active["user_id"], session_id="OLD1")
        return active, idle, old, new, idle_s

    def test_dry_run_lists_but_deletes_nothing(self):
        active, idle, old, new, idle_s = self._fixture()
        report = retention.run(self.settings, dry_run=True)
        self.assertEqual([r["session_id"] for r in report["raw_video_deleted"]], ["OLD1"])
        self.assertEqual(report["raw_video_deleted"][0]["files"], 6)  # 4 in session + 2 GEM-X extras
        self.assertEqual([a["user_id"] for a in report["accounts_deleted"]], [idle["user_id"]])
        self.assertTrue((old / "upload" / "source_video.mp4").is_file())
        self.assertTrue((self.gemx / "OLD1" / "preprocess" / "0_input_video.mp4").is_file())
        self.assertTrue(idle_s.is_dir())
        self.assertTrue(profile(self.settings.product_dir, idle["user_id"]))
        self.assertFalse((self.settings.product_dir / "retention.jsonl").exists())
        self.assertIn("would delete", retention.summary_line(report))

    def test_real_run_deletes_raw_video_and_inactive_accounts(self):
        active, idle, old, new, idle_s = self._fixture()
        report = retention.run(self.settings)
        # Raw video of the old capture is gone, derived results stay.
        for rel in ("upload", "jpegs", "ik/session.mp4", "ik/poster.jpg"):
            self.assertFalse((old / rel).exists(), rel)
        self.assertTrue((old / "ik" / "opensim-motion.json").is_file())
        self.assertTrue((old / "ik" / "pose3d.json").is_file())
        self.assertTrue((self.gemx / "OLD1" / "hpe_results.pt").is_file())
        self.assertFalse((self.gemx / "OLD1" / "preprocess").exists())
        marker = json.loads((old / "retention.json").read_text())
        self.assertEqual(marker["raw_video_retention_days"], 30)
        # The recent capture is untouched.
        self.assertTrue((new / "upload" / "source_video.mp4").is_file())
        self.assertTrue((new / "jpegs" / "000000.jpg").is_file())
        # The inactive account and everything it owned is gone.
        self.assertEqual(profile(self.settings.product_dir, idle["user_id"]), {})
        self.assertFalse(idle_s.exists())
        self.assertFalse((self.gemx / "IDLE1").exists())
        self.assertFalse(any(r["user_id"] == idle["user_id"] for r in all_captures(self.settings.product_dir)))
        events = (self.settings.product_dir / "events.jsonl").read_text()
        self.assertNotIn(idle["user_id"], events)
        self.assertIn(active["user_id"], events)
        # The run is logged; a second run finds nothing more.
        log = (self.settings.product_dir / "retention.jsonl").read_text().splitlines()
        self.assertEqual(len(log), 1)
        self.assertEqual(json.loads(log[0])["raw_video_deleted"][0]["session_id"], "OLD1")
        again = retention.run(self.settings)
        self.assertEqual(again["raw_video_deleted"], [])
        self.assertEqual(again["accounts_deleted"], [])

    def test_env_overrides_and_rewritten_results_do_not_extend_the_clock(self):
        active = self._user(consent=True)
        s = self._session("MID1", processed_days_ago=10, owner=active["user_id"])
        self.assertEqual(retention.run(self.settings, dry_run=True)["raw_video_deleted"], [])
        with mock.patch.dict(os.environ, {"CEREBEL_RAW_VIDEO_RETENTION_DAYS": "7"}):
            self.assertEqual(len(retention.run(self.settings, dry_run=True)["raw_video_deleted"]), 1)
        os.utime(s / "ik" / "opensim-motion.json", None)  # result rewritten today
        with mock.patch.dict(os.environ, {"CEREBEL_RAW_VIDEO_RETENTION_DAYS": "7"}):
            self.assertEqual(len(retention.run(self.settings, dry_run=True)["raw_video_deleted"]), 1)

    def test_paying_inactive_account_is_skipped_and_demo_is_kept(self):
        from motion_agent.app import billing

        payer = self._user("payer@example.com", consent=True)
        self._session("PAY1", processed_days_ago=800, owner=payer["user_id"])
        self._make_inactive(payer["user_id"], 800)
        data = billing._read(self.settings.product_dir)
        data["users"][payer["user_id"]] = {"status": "active", "current_period_end": time.time() + 86400 * 30}
        billing._write(self.settings.product_dir, data)
        demo = self._session("DEMO1", processed_days_ago=900, owner=None)
        with mock.patch.dict(os.environ, {"CEREBEL_DEMO_SESSION_ID": "DEMO1"}):
            report = retention.run(self.settings)
        self.assertEqual([a["user_id"] for a in report["accounts_skipped"]], [payer["user_id"]])
        self.assertTrue(profile(self.settings.product_dir, payer["user_id"]))
        self.assertTrue((demo / "upload" / "source_video.mp4").is_file())

    def test_unowned_and_orphan_data(self):
        stale = self._session("LEGACY1", processed_days_ago=800, owner=None)
        (self.gemx / "GONE1").mkdir()
        (self.gemx / "GONE1" / "hpe_results.pt").write_bytes(b"x")
        _age(self.gemx / "GONE1", 60)
        report = retention.run(self.settings)
        self.assertEqual(report["orphan_sessions_deleted"], ["LEGACY1"])
        self.assertIn("GONE1", report["orphan_gemx_deleted"])
        self.assertFalse(stale.exists())
        self.assertFalse((self.gemx / "GONE1").exists())

    def test_neck_ik_leftovers_outside_the_session_are_removed(self):
        active = self._user(consent=True)
        self._session("RECENT1", processed_days_ago=2, owner=active["user_id"])
        self._session("AGED1", processed_days_ago=40, owner=active["user_id"])
        for sid, days in (("RECENT1", 2), ("AGED1", 40), ("GONE2", 1)):
            (self.neck / sid).mkdir()
            (self.neck / sid / "soma_raw.npz").write_bytes(b"x")
            (self.neck / sid / "soma_aligned.npz").write_bytes(b"x")
            _age(self.neck / sid, days)
        dry = retention.run(self.settings, dry_run=True)
        self.assertEqual(sorted(dry["neck_ik_deleted"]), ["AGED1", "GONE2"])
        self.assertTrue((self.neck / "GONE2").is_dir())
        report = retention.run(self.settings)
        self.assertEqual(sorted(report["neck_ik_deleted"]), ["AGED1", "GONE2"])
        self.assertTrue((self.neck / "RECENT1" / "soma_aligned.npz").is_file())
        self.assertFalse((self.neck / "AGED1").exists())
        self.assertFalse((self.neck / "GONE2").exists())
        self.assertIn("2 neck IK folders", retention.summary_line(report))

    def test_cli_dry_run(self):
        self._session("CLI1", processed_days_ago=45, owner=None)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CEREBEL_DATA_DIR": str(self.data)}), mock.patch("sys.stdout", out):
            self.assertEqual(retention.main(["--dry-run", "--json"]), 0)
        report = json.loads(out.getvalue())
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["raw_video_deleted"][0]["session_id"], "CLI1")
        self.assertTrue((self.data / "sessions" / "CLI1" / "upload" / "source_video.mp4").is_file())


class AccountDeleteTests(_TempData):
    def test_self_delete_removes_everything(self):
        from motion_agent.app import billing

        user = self._user(consent=True)
        uid = user["user_id"]
        for sid in ("DEL1", "DEL2"):
            s = self.data / "sessions" / sid / "upload"
            s.mkdir(parents=True)
            (s / "source_video.mp4").write_bytes(b"v")
            (self.gemx / sid).mkdir()
            (self.gemx / sid / "hpe_results.pt").write_bytes(b"x")
            (self.neck / sid).mkdir()
            (self.neck / sid / "soma_aligned.npz").write_bytes(b"x")
            add_capture(self.settings.product_dir, user_id=uid, session_id=sid, filename="mine.mp4")
        product_api.remove_capture(self.settings, user, "DEL2")
        (self.gemx / "DEL2").mkdir()  # simulate GPU results left over from before this fix
        data = billing._read(self.settings.product_dir)
        data["users"][uid] = {"status": "canceled", "customer": "cus_1"}
        billing._write(self.settings.product_dir, data)
        product_api.record_event(self.settings, "upload_saved", user_id=uid, session_id="DEL1")

        code, payload = product_api.remove_account(self.settings, user)
        self.assertEqual(code, 200)
        self.assertEqual(payload["deleted_captures"], 1)
        self.assertIsNone(user_from_token(self.settings.product_dir, user["token"]))
        self.assertEqual(profile(self.settings.product_dir, uid), {})
        for sid in ("DEL1", "DEL2"):
            self.assertFalse((self.data / "sessions" / sid).exists())
            self.assertFalse((self.gemx / sid).exists())
            self.assertFalse((self.neck / sid).exists())
        self.assertEqual(all_captures(self.settings.product_dir), [])
        self.assertNotIn(uid, billing._read(self.settings.product_dir)["users"])
        events = (self.settings.product_dir / "events.jsonl").read_text()
        self.assertNotIn(uid, events)
        self.assertNotIn("DEL1", events)
        self.assertIn("account_deleted", events)

    def test_delete_blocked_while_subscription_renews(self):
        from motion_agent.app import billing

        user = self._user()
        data = billing._read(self.settings.product_dir)
        data["users"][user["user_id"]] = {"status": "active", "current_period_end": time.time() + 86400}
        billing._write(self.settings.product_dir, data)
        code, payload = product_api.remove_account(self.settings, user)
        self.assertEqual(code, 409)
        self.assertTrue(payload["subscription_active"])
        self.assertTrue(profile(self.settings.product_dir, user["user_id"]))
        data["users"][user["user_id"]]["cancel_at_period_end"] = True
        billing._write(self.settings.product_dir, data)
        self.assertEqual(product_api.remove_account(self.settings, user)[0], 200)


class HpeSanitizeTests(unittest.TestCase):
    def setUp(self):
        try:
            import torch
        except ImportError:
            raise unittest.SkipTest("torch not installed")
        self.torch = torch
        import sys

        root = str(Path(__file__).resolve().parents[2])
        if root not in sys.path:
            sys.path.insert(0, root)
        import hpe_privacy

        self.hpe_privacy = hpe_privacy

    def _pred(self):
        t = self.torch
        params = lambda: {  # noqa: E731
            "global_orient": t.ones(5, 3),
            "body_pose": t.ones(5, 228),
            "identity_coeffs": t.arange(45.0).repeat(5, 1) + 1,
            "scale_params": t.ones(5, 75),
            "transl": t.ones(5, 3),
        }
        return {
            "body_params_global": params(),
            "body_params_incam": params(),
            "K_fullimg": t.eye(3).repeat(5, 1, 1),
            "net_outputs": {"model_output": {"pred_x": t.ones(5, 600)}, "decode_dict": {}},
        }

    def test_drops_head_shape_and_network_outputs_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hpe_results.pt"
            original = self._pred()
            self.torch.save(original, path)
            self.assertTrue(self.hpe_privacy.sanitize_file(path))
            out = self.torch.load(path, weights_only=False)
            self.assertNotIn("net_outputs", out)
            self.assertTrue(out["cerebel_privacy"]["head_identity_zeroed"])
            for key in ("body_params_global", "body_params_incam"):
                ident = out[key]["identity_coeffs"]
                self.assertTrue(self.torch.all(ident[:, 20:40] == 0))
                self.assertTrue(self.torch.equal(ident[:, :20], original[key]["identity_coeffs"][:, :20]))
                self.assertTrue(self.torch.equal(ident[:, 40:], original[key]["identity_coeffs"][:, 40:]))
                self.assertTrue(self.torch.equal(out[key]["body_pose"], original[key]["body_pose"]))
            self.assertTrue(self.torch.equal(out["K_fullimg"], original["K_fullimg"]))
            self.assertFalse(self.hpe_privacy.sanitize_file(path))
            self.assertEqual(self.hpe_privacy.main(["--root", tmp]), 0)


class LegalPageTests(unittest.TestCase):
    def test_privacy_page_fills_retention_and_names_processors(self):
        try:
            from motion_agent.app.chat_server import _legal_html
        except Exception as exc:  # noqa: BLE001 - server deps missing on this machine
            raise unittest.SkipTest(f"chat_server not importable: {exc}")
        with mock.patch.dict(os.environ, {"CEREBEL_RAW_VIDEO_RETENTION_DAYS": "30", "CEREBEL_INACTIVE_DELETE_DAYS": "730"}):
            html = _legal_html("privacy").decode("utf-8")
        self.assertNotIn("{{", html)
        for word in ("Modal", "DigitalOcean", "Stripe", "Together AI", "Cloudflare", "SpaceMail",
                     "30 days", "24 months", "Illinois", "Texas", "Washington", "Colorado", "California"):
            self.assertIn(word, html)


if __name__ == "__main__":
    unittest.main()
