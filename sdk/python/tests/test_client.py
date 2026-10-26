"""CerebelClient against the in-process mock API: the whole account → upload → report journey."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cerebel import (
    AnalysisFailed,
    AuthError,
    CerebelClient,
    ConsentRequired,
    Forbidden,
    PaymentRequired,
    ServiceUnavailable,
    UploadRejected,
    main_fix,
)
from cerebel.testing import MockCerebelServer


def _video(folder: Path, name: str = "swing.mp4", size: int = 4096) -> Path:
    path = folder / name
    path.write_bytes(b"\x00" * size)
    return path


class ClientJourneyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = MockCerebelServer(stage_seconds=0.05).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.client = CerebelClient(self.mock.url)

    def tearDown(self) -> None:
        self.mock.stop()
        self.tmp.cleanup()

    def _signed_in(self, email: str = "dev@example.com") -> CerebelClient:
        self.client.register(email, "password123", accept_terms=True)
        return self.client

    def test_register_requires_terms(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self.client.register("a@example.com", "password123", accept_terms=False)
        self.assertEqual(getattr(ctx.exception, "status", None), 400)

    def test_register_sets_token_and_me(self) -> None:
        out = self._signed_in()
        self.assertTrue(self.client.token)
        me = out.me()
        self.assertEqual(me["user"]["email"], "dev@example.com")
        self.assertEqual(me["quota"]["remaining"], 3)
        self.assertTrue(me["video_consent"]["required"])

    def test_wrong_password_is_auth_error(self) -> None:
        self._signed_in()
        with self.assertRaises(AuthError):
            CerebelClient(self.mock.url).login("dev@example.com", "nope")

    def test_upload_needs_consent_then_succeeds(self) -> None:
        client = self._signed_in()
        video = _video(self.folder)
        with self.assertRaises(ConsentRequired) as ctx:
            client.upload(video)
        self.assertEqual(ctx.exception.consent["version"], "2026-10-24-us")
        client.accept_video_consent()
        started = client.upload(video)
        self.assertEqual(started["stage"], "extracting")
        self.assertTrue(started["warnings"])

    def test_analyze_waits_for_sport_stamp_and_returns_main_fix(self) -> None:
        client = self._signed_in()
        client.accept_video_consent()
        seen: list[str] = []
        result = client.analyze(
            _video(self.folder), poll_interval=0.01, on_progress=lambda p: seen.append(str(p.get("stage")))
        )
        self.assertEqual(result["progress"]["sport"], "golf")
        self.assertEqual(result["main_fix"]["id"], "trail-knee")
        self.assertIn("queued", seen)
        self.assertEqual(seen[-1], "ready")

    def test_no_swing_raises_analysis_failed(self) -> None:
        client = self._signed_in()
        client.accept_video_consent()
        started = client.upload(_video(self.folder, "noswing.mp4"))
        with self.assertRaises(AnalysisFailed) as ctx:
            client.wait_for_analysis(started["session_id"], poll_interval=0.01)
        self.assertEqual(ctx.exception.stage, "no_swing")

    def test_quality_rejection(self) -> None:
        client = self._signed_in()
        client.accept_video_consent()
        with self.assertRaises(UploadRejected):
            client.upload(_video(self.folder, "short.mp4"))

    def test_quota_runs_out(self) -> None:
        client = self._signed_in()
        client.accept_video_consent()
        for _ in range(3):
            client.analyze(_video(self.folder), poll_interval=0.01)
        with self.assertRaises(PaymentRequired) as ctx:
            client.upload(_video(self.folder))
        self.assertEqual(ctx.exception.quota["remaining"], 0)

    def test_gpu_down_is_service_unavailable(self) -> None:
        self.mock.state.gpu = "down"
        client = self._signed_in()
        client.accept_video_consent()
        self.assertFalse(client.health()["ok"])
        with self.assertRaises(ServiceUnavailable):
            client.upload(_video(self.folder))

    def test_other_accounts_capture_is_private(self) -> None:
        client = self._signed_in()
        client.accept_video_consent()
        sid = client.analyze(_video(self.folder), poll_interval=0.01)["session_id"]
        other = CerebelClient(self.mock.url)
        other.register("other@example.com", "password123", accept_terms=True)
        with self.assertRaises(Forbidden):
            other.sport_analysis(sid)

    def test_reports_compare_practice_and_delete(self) -> None:
        client = self._signed_in()
        client.accept_video_consent()
        a = client.analyze(_video(self.folder), poll_interval=0.01)["session_id"]
        b = client.analyze(_video(self.folder), poll_interval=0.01)["session_id"]
        self.assertEqual(client.golf_phases(b)["swing_count"], 1)
        self.assertTrue(client.coach_briefs(b)["briefs"])
        self.assertEqual(client.session(b)["sport"], "golf")
        cmp = client.compare(a, b)
        self.assertEqual(cmp["main_issue"]["status"], "improved")
        self.assertEqual(client.log_practice(b, "trail-knee")["count"], 1)
        self.assertEqual(client.log_practice(b, "trail-knee")["count"], 2)
        client.delete_capture(a)
        self.assertNotIn(a, [row["session_id"] for row in client.library()["captures"]])

    def test_demo_is_public(self) -> None:
        session = self.client.session()
        self.assertTrue(session["demo"])
        self.assertEqual(main_fix(self.client.sport_analysis("DEMO"))["headline"], "Your back knee locks too early")

    def test_ask_and_stream(self) -> None:
        answer = self.client.ask("What is the biggest fix?")
        self.assertIn("back knee", answer["answer"])
        events = list(self.client.ask_stream("What is the biggest fix?"))
        self.assertEqual([e["type"] for e in events][-1], "done")
        self.assertIn("back knee", events[-1]["answer"])

    def test_media_url_joins_origin(self) -> None:
        client = CerebelClient("https://cerebel.tech/playground")
        self.assertEqual(
            client.media_url("/playground/media/ABC/session.mp4?mt=1"),
            "https://cerebel.tech/playground/media/ABC/session.mp4?mt=1",
        )
        self.assertEqual(client._url("/api/me"), "https://cerebel.tech/playground/api/me")

    def test_download_media(self) -> None:
        dest = self.client.download_media("/media/DEMO/session.mp4?mt=x", self.folder / "v.mp4")
        self.assertTrue(dest.read_bytes().startswith(b"\x00\x00\x00\x18ftyp"))

    def test_token_is_not_sent_to_other_origins(self) -> None:
        client = CerebelClient("https://cerebel.tech/playground", "tok", upload_base_url="https://up.example.test")
        self.assertTrue(client._same_origin("https://cerebel.tech/playground/media/A/v.mp4"))
        self.assertTrue(client._same_origin("https://up.example.test/media/A/v.mp4"))
        self.assertFalse(client._same_origin("https://evil.example/media/A/v.mp4"))
        self.assertFalse(client._same_origin("http://cerebel.tech/media/A/v.mp4"))

    def test_unreachable_server_is_cerebel_error(self) -> None:
        from cerebel import CerebelError

        dead = CerebelClient("http://127.0.0.1:9", timeout=2)
        with self.assertRaises(CerebelError) as ctx:
            dead.health()
        self.assertEqual(ctx.exception.status, 0)


if __name__ == "__main__":
    unittest.main()
