"""One-off clean-up of what accounts deleted by older builds left behind, and hpe_results.pt backfill."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from motion_agent.app import privacy_backfill, product_api
from motion_agent.app.accounts import delete_account
from motion_agent.app.library import add_capture, all_captures
from motion_agent.tests.test_privacy_retention import _TempData, _age


class DeletedAccountTests(_TempData):
    def _capture(self, user_id: str, sid: str) -> None:
        upload = self.data / "sessions" / sid / "upload"
        upload.mkdir(parents=True)
        (upload / "source_video.mp4").write_bytes(b"v")
        (self.gemx / sid).mkdir()
        (self.gemx / sid / "hpe_results.pt").write_bytes(b"x")
        (self.neck / sid).mkdir()
        (self.neck / sid / "soma_aligned.npz").write_bytes(b"x")
        add_capture(self.settings.product_dir, user_id=user_id, session_id=sid, filename="a.mp4")
        product_api.record_event(self.settings, "upload_saved", user_id=user_id, session_id=sid)

    def _old_style_delete(self, user_id: str) -> None:
        """What remove_account did before the fix: account and session folders only."""
        for row in all_captures(self.settings.product_dir):
            if row["user_id"] == user_id:
                shutil.rmtree(self.data / "sessions" / row["session_id"], ignore_errors=True)
        delete_account(self.settings.product_dir, user_id)
        product_api.record_event(self.settings, "account_deleted", user_id=user_id, captures=1)

    def _fixture(self):
        live = self._user("live@example.com", consent=True)
        gone = self._user("gone@example.com", consent=True)
        quiet = self._user("quiet@example.com")
        self._capture(live["user_id"], "LIVE1")
        self._capture(gone["user_id"], "GONE1")
        demo = self.data / "sessions" / "DEMO1"
        (demo / "ik").mkdir(parents=True)
        (self.gemx / "DEMO1").mkdir()
        rec = product_api.record_event
        rec(self.settings, "signup", user_id=gone["user_id"])
        rec(self.settings, "chat_question", user_id=gone["user_id"], session_id="GONE1")
        rec(self.settings, "chat_question", user_id=gone["user_id"], session_id="DEMO1")
        rec(self.settings, "chat_question", user_id=gone["user_id"], session_id="LIVE1")
        rec(self.settings, "pipeline_error", session_id="GONE1", message="x")
        rec(self.settings, "chat_question", user_id=live["user_id"], session_id="LIVE1")
        rec(self.settings, "chat_question", session_id="DEMO1")
        self._old_style_delete(gone["user_id"])
        self._old_style_delete(quiet["user_id"])
        return live, gone, quiet

    def _events(self) -> list[dict]:
        path = self.settings.product_dir / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_finds_deleted_accounts_and_only_their_own_captures(self):
        live, gone, quiet = self._fixture()
        with mock.patch.dict(os.environ, {"CEREBEL_DEMO_SESSION_ID": "DEMO1"}):
            found = privacy_backfill.deleted_accounts(self.settings)
        self.assertEqual(found, {gone["user_id"]: ["GONE1"], quiet["user_id"]: []})

    def test_report_only_changes_nothing(self):
        live, gone, quiet = self._fixture()
        before = (self.settings.product_dir / "events.jsonl").read_text()
        with mock.patch.dict(os.environ, {"CEREBEL_DEMO_SESSION_ID": "DEMO1"}):
            report = privacy_backfill.run(self.settings, hpe=False)
        rows = {r["user_id"]: r for r in report["deleted_accounts"]}
        self.assertEqual(rows[gone["user_id"]]["session_ids"], ["GONE1"])
        self.assertEqual(rows[gone["user_id"]]["gemx_dirs"], 1)
        self.assertEqual(rows[gone["user_id"]]["neck_dirs"], 1)
        self.assertEqual(rows[gone["user_id"]]["session_dirs"], 0)
        self.assertEqual(rows[gone["user_id"]]["library_rows"], 1)
        # upload_saved, signup, 3 questions, pipeline_error (by session); not account_deleted
        self.assertEqual(rows[gone["user_id"]]["events"], 6)
        self.assertEqual(rows[gone["user_id"]]["deletion_rows_anonymized"], 1)
        self.assertEqual(rows[quiet["user_id"]]["events"], 0)
        self.assertEqual(rows[quiet["user_id"]]["deletion_rows_anonymized"], 1)
        self.assertTrue((self.gemx / "GONE1" / "hpe_results.pt").is_file())
        self.assertTrue((self.neck / "GONE1").is_dir())
        self.assertEqual((self.settings.product_dir / "events.jsonl").read_text(), before)
        self.assertEqual(len(all_captures(self.settings.product_dir)), 2)
        self.assertFalse((self.settings.product_dir / "privacy_backfill.jsonl").exists())
        self.assertIn("would do: 2 deleted accounts: 1 captures", privacy_backfill.summary_line(report))

    def test_apply_removes_leftovers_and_keeps_everything_else(self):
        live, gone, quiet = self._fixture()
        with mock.patch.dict(os.environ, {"CEREBEL_DEMO_SESSION_ID": "DEMO1"}):
            report = privacy_backfill.run(self.settings, apply=True, hpe=False)
        self.assertEqual(sum(r["events"] for r in report["deleted_accounts"]), 6)
        self.assertFalse((self.gemx / "GONE1").exists())
        self.assertFalse((self.neck / "GONE1").exists())
        self.assertEqual([r["session_id"] for r in all_captures(self.settings.product_dir)], ["LIVE1"])
        events = self._events()
        text = json.dumps(events)
        for uid in (gone["user_id"], quiet["user_id"]):
            self.assertNotIn(uid, text)
        self.assertNotIn("GONE1", text)
        self.assertEqual(sum(e["kind"] == "account_deleted" for e in events), 2)
        # The live account, its capture and the demo are untouched.
        self.assertIn(live["user_id"], text)
        self.assertEqual(sum(e.get("session_id") == "LIVE1" for e in events), 2)  # upload + own question
        self.assertTrue((self.data / "sessions" / "LIVE1" / "upload" / "source_video.mp4").is_file())
        self.assertTrue((self.gemx / "LIVE1").is_dir() and (self.neck / "LIVE1").is_dir())
        self.assertTrue((self.data / "sessions" / "DEMO1").is_dir() and (self.gemx / "DEMO1").is_dir())
        self.assertTrue(any(e.get("session_id") == "DEMO1" for e in events))  # anonymous demo question
        log = (self.settings.product_dir / "privacy_backfill.jsonl").read_text().splitlines()
        self.assertEqual(len(log), 1)
        self.assertNotIn("@", log[0])
        again = privacy_backfill.run(self.settings, apply=True, hpe=False)
        self.assertEqual(again["deleted_accounts"], [])

    def test_cli_report_only(self):
        self._fixture()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CEREBEL_DATA_DIR": str(self.data)}), mock.patch("sys.stdout", out):
            self.assertEqual(privacy_backfill.main(["--json", "--skip-hpe"]), 0)
        report = json.loads(out.getvalue())
        self.assertFalse(report["applied"])
        self.assertEqual(len(report["deleted_accounts"]), 2)
        self.assertNotIn("hpe_results", report)
        self.assertNotIn("@", out.getvalue())
        self.assertTrue((self.gemx / "GONE1").is_dir())


class HpeBackfillTests(unittest.TestCase):
    def setUp(self):
        try:
            import torch
        except ImportError:
            raise unittest.SkipTest("torch not installed")
        self.torch = torch
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for sid in ("OLD1", "OLD2", "NEW1"):
            (self.root / sid).mkdir()
            torch.save(self._pred(), self.root / sid / "hpe_results.pt")
        _age(self.root / "OLD1", 3)
        _age(self.root / "OLD2", 3)

    def tearDown(self):
        self._tmp.cleanup()

    def _pred(self):
        t = self.torch
        params = {"body_pose": t.ones(2, 228), "identity_coeffs": t.arange(45.0).repeat(2, 1) + 1}
        return {"body_params_global": dict(params), "body_params_incam": dict(params),
                "K_fullimg": t.eye(3).repeat(2, 1, 1), "net_outputs": {"pred_x": t.ones(2, 600)}}

    def test_report_then_apply_then_idempotent(self):
        hour = timedelta(hours=1)
        before = (self.root / "OLD1" / "hpe_results.pt").read_bytes()
        dry = privacy_backfill.sanitize_hpe_results(self.root, apply=False, min_age=hour)
        self.assertEqual(dry["sanitized"], ["OLD1", "OLD2"])
        self.assertEqual(dry["skipped_recent"], ["NEW1"])
        self.assertEqual((self.root / "OLD1" / "hpe_results.pt").read_bytes(), before)

        done = privacy_backfill.sanitize_hpe_results(self.root, apply=True, min_age=hour)
        self.assertEqual(done["sanitized"], ["OLD1", "OLD2"])
        out = self.torch.load(self.root / "OLD1" / "hpe_results.pt", weights_only=False)
        self.assertNotIn("net_outputs", out)
        self.assertTrue(self.torch.all(out["body_params_global"]["identity_coeffs"][:, 20:40] == 0))
        self.assertIn("net_outputs", self.torch.load(self.root / "NEW1" / "hpe_results.pt", weights_only=False))

        again = privacy_backfill.sanitize_hpe_results(self.root, apply=False, min_age=hour)
        self.assertEqual((again["sanitized"], again["already_clean"]), ([], 2))
        everything = privacy_backfill.sanitize_hpe_results(self.root, apply=True, min_age=timedelta(0))
        self.assertEqual((everything["sanitized"], everything["already_clean"]), (["NEW1"], 2))

    def test_unreadable_file_is_reported_and_the_rest_continue(self):
        (self.root / "OLD1" / "hpe_results.pt").write_bytes(b"not a torch file")
        _age(self.root / "OLD1", 3)
        done = privacy_backfill.sanitize_hpe_results(self.root, apply=True, min_age=timedelta(hours=1))
        self.assertEqual([f["session_id"] for f in done["failed"]], ["OLD1"])
        self.assertEqual(done["sanitized"], ["OLD2"])


if __name__ == "__main__":
    unittest.main()
