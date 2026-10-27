"""The "is this really a golf swing" gate: each rule, other sports, and real captures."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from motion_agent.llm.agent import _summarize_tool_result
from motion_agent.motion.golf_phases import segment_golf_swings
from motion_agent.sports.packs import golf_gate
from motion_agent.sports.packs.golf import GolfPack
from motion_agent.sports.packs.golf_gate import (
    COMING_SOON,
    NO_SWING_MESSAGE,
    TOO_SHORT_MESSAGE,
    evaluate_golf_gate,
)
from motion_agent.tests import synthetic_motion as sm

REPO = Path(__file__).resolve().parents[2]
# Real captures in the repo: two golf swings (old and new body model each) and the
# kinetic-lens layup, which the old detector called golf at 0.71.
GOLF_CAPTURES = sorted((REPO / "kinetic-lens" / "retargeting_neck_v2" / "outputs").glob("*/*/opensim-motion.json"))
LAYUP_CAPTURE = REPO / "kinetic-lens" / "public" / "assets" / "model" / "opensim-motion.json"


def _phases(seq) -> dict:
    return _summarize_tool_result("segment_golf_swings", segment_golf_swings(seq, max_swings=5))


def _gate(seq) -> dict:
    return evaluate_golf_gate(seq, _phases(seq))


def _failed(gate: dict) -> set[str]:
    return {c["id"] for c in gate["checks"] if not c["passed"]}


def _place(data: Path, sid: str, motion: Path) -> Path:
    session = data / "sessions" / sid
    (session / "ik").mkdir(parents=True)
    (session / "ik" / "opensim-motion.json").symlink_to(motion)
    return session


class GateRuleTests(unittest.TestCase):
    """Each golf variant breaks one trait; exactly that rule must catch it."""

    def test_synthetic_golf_swing_passes(self):
        gate = _gate(sm.golf_swing())
        self.assertTrue(gate["passed"], gate)
        self.assertEqual(gate["message"], "")
        self.assertEqual(_failed(gate), set())

    def test_clip_length(self):
        gate = _gate(sm.golf_swing(duration_s=0.7, time_scale=0.25))
        self.assertEqual(gate["reason"], "clip_length")
        self.assertEqual(_failed(gate), {"clip_length"})
        self.assertFalse(gate["not_golf"])
        self.assertEqual(gate["message"], TOO_SHORT_MESSAGE)

    def test_swing_speed(self):
        gate = _gate(sm.arm_wave())
        self.assertEqual(gate["reason"], "swing_speed")
        self.assertFalse(gate["not_golf"], "an arm wave is no swing, not another sport")
        self.assertIsNone(gate["looks_like"])
        self.assertEqual(gate["message"], NO_SWING_MESSAGE)

    def test_two_hands(self):
        gate = _gate(sm.golf_swing(release_lead_hand=True))
        self.assertEqual(_failed(gate), {"two_hands"})
        self.assertTrue(gate["not_golf"])

    def test_stays_grounded(self):
        gate = _gate(sm.golf_swing(jump_m=0.3))
        self.assertEqual(_failed(gate), {"stays_grounded"})

    def test_stance_planted(self):
        gate = _gate(sm.golf_swing(slide_m=0.4))
        self.assertEqual(_failed(gate), {"stance_planted"})
        self.assertIn("both feet moved", gate["message"])
        self.assertIn("If it was a golf swing", gate["message"])

    def test_address_hands_low(self):
        gate = _gate(sm.golf_swing(address_lift_m=0.25))
        self.assertEqual(_failed(gate), {"address_hands_low"})

    def test_impact_hands_low(self):
        gate = _gate(sm.golf_swing(impact_lift_m=0.4))
        self.assertEqual(_failed(gate), {"impact_hands_low"})

    def test_one_passing_swing_is_enough(self):
        seq = sm.golf_swing()
        payload = _phases(seq)
        good = payload["swings"][0]
        bogus = dict(good, swing_index=1, confidence=0.99, start_time=2.5, impact_time=2.8, end_time=2.9, phases=[])
        payload["swings"] = [bogus, good]
        gate = evaluate_golf_gate(seq, payload)
        self.assertTrue(gate["passed"], gate)
        self.assertEqual(gate["swing_index"], good["swing_index"])

    def test_no_segmented_swing(self):
        gate = evaluate_golf_gate(sm.golf_swing(), {"error": True, "type": "INSUFFICIENT_WINDOW"})
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "no_swing")
        self.assertEqual(gate["message"], NO_SWING_MESSAGE)

    def test_missing_joints_leaves_the_old_decision(self):
        seq = sm.golf_swing()
        payload = _phases(seq)
        del seq.joint_positions["left_ankle"]
        gate = evaluate_golf_gate(seq, payload)
        self.assertTrue(gate["passed"])
        self.assertIn("skipped", gate["note"])


class OtherSportsTests(unittest.TestCase):
    """The segmenter calls all of these golf; the gate must not."""

    def _assert_not_golf(self, seq, looks_like: str, phrase: str) -> dict:
        self.assertGreaterEqual(
            max(s["confidence"] for s in _phases(seq)["swings"]), 0.45, "old detector would have said golf"
        )
        gate = _gate(seq)
        self.assertFalse(gate["passed"], gate)
        self.assertTrue(gate["not_golf"])
        self.assertEqual(gate["looks_like"], looks_like)
        self.assertIn("doesn't look like a golf swing", gate["message"])
        self.assertIn(phrase, gate["message"])
        self.assertIn(COMING_SOON, gate["message"])
        return gate

    def test_one_handed_racket_swing(self):
        gate = self._assert_not_golf(sm.tennis_forehand(), "one_handed", "a one-handed swing or throw")
        self.assertIn("two_hands", _failed(gate))

    def test_overhead_serve(self):
        gate = self._assert_not_golf(sm.tennis_serve(), "overhead", "an overhead swing or throw")
        self.assertIn("impact_hands_low", _failed(gate))

    def test_basketball_layup(self):
        gate = self._assert_not_golf(sm.basketball_layup(), "run_or_jump", "running or jumping")
        self.assertTrue({"stays_grounded", "stance_planted"} <= _failed(gate))

    def test_bat_swing(self):
        gate = self._assert_not_golf(sm.bat_swing(), "bat_swing", "a baseball or softball swing")
        self.assertNotIn("two_hands", _failed(gate), "a bat is held in two hands too")
        self.assertIn("address_hands_low", _failed(gate))


class GolfPackDetectTests(unittest.TestCase):
    def _detect(self, seq):
        with mock.patch("motion_agent.llm.agent.get_golf_phase_cards", return_value=_phases(seq)), mock.patch(
            "motion_agent.motion.adapter.adapt_session", return_value=seq
        ) as load:
            result = GolfPack().detect(Path("/tmp/none/sessions/S1"), video_id="S1", data_dir=Path("/tmp/none"))
        return result, load

    def test_golf_swing_matches(self):
        result, _ = self._detect(sm.golf_swing())
        self.assertTrue(result.matched)
        self.assertEqual(result.message, "")
        self.assertTrue(result.detail["gate"]["passed"])

    def test_racket_swing_is_refused_with_a_reason(self):
        result, _ = self._detect(sm.tennis_forehand())
        self.assertFalse(result.matched)
        self.assertTrue(result.detail["not_golf"])
        self.assertEqual(result.detail["looks_like"], "one_handed")
        self.assertIn("one-handed swing", result.message)

    def test_low_segment_confidence_skips_the_gate(self):
        payload = {"ok": True, "swings": [{"confidence": 0.3, "phases": []}]}
        with mock.patch("motion_agent.llm.agent.get_golf_phase_cards", return_value=payload), mock.patch(
            "motion_agent.motion.adapter.adapt_session"
        ) as load:
            result = GolfPack().detect(Path("/tmp/none/sessions/S1"), video_id="S1", data_dir=Path("/tmp/none"))
        self.assertFalse(result.matched)
        self.assertEqual(result.message, NO_SWING_MESSAGE)
        load.assert_not_called()

    def test_registry_fallback_keeps_the_golf_reason(self):
        from motion_agent.sports.registry import detect_sport

        seq = sm.tennis_serve()
        with mock.patch("motion_agent.llm.agent.get_golf_phase_cards", return_value=_phases(seq)), mock.patch(
            "motion_agent.motion.adapter.adapt_session", return_value=seq
        ):
            pack, result = detect_sport(Path("/tmp/none/sessions/S1"), video_id="S1", data_dir=Path("/tmp/none"))
        self.assertEqual(pack.id, "general")
        self.assertIn("overhead swing", result.message)
        self.assertEqual(result.detail["rejected_pack"], "golf")


@unittest.skipUnless(GOLF_CAPTURES and LAYUP_CAPTURE.is_file(), "real captures not in this checkout")
class RealCaptureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"

    def tearDown(self):
        self._tmp.cleanup()

    def _detect(self, sid: str, motion: Path):
        session = _place(self.data, sid, motion)
        return GolfPack().detect(session, video_id=sid, data_dir=self.data)

    def test_real_golf_swings_still_match(self):
        self.assertEqual(len(GOLF_CAPTURES), 4)
        for i, motion in enumerate(GOLF_CAPTURES):
            with self.subTest(capture=str(motion.relative_to(REPO))):
                result = self._detect(f"GOLF{i}", motion)
                self.assertTrue(result.matched, result.detail.get("gate"))

    def test_layup_is_no_longer_golf(self):
        result = self._detect("LAYUP", LAYUP_CAPTURE)
        self.assertGreaterEqual(result.confidence, 0.45, "the segmenter alone still calls it golf")
        self.assertFalse(result.matched)
        self.assertEqual(result.detail["looks_like"], "run_or_jump")
        self.assertIn("running or jumping", result.message)

    def test_finalize_files_the_layup_as_uncounted(self):
        from motion_agent.app.billing import _NOT_COUNTED
        from motion_agent.app.config import Settings
        from motion_agent.app.library import add_capture, list_captures
        from motion_agent.app.product_api import NOT_COUNTED_NOTE, finalize_capture

        _place(self.data, "LAYUP", LAYUP_CAPTURE)
        settings = Settings(
            data_dir=self.data,
            host="127.0.0.1",
            port=1,
            backend="mock",
            prompt_password=None,
            max_upload_bytes=1024,
            invite_daily_limit=3,
        )
        add_capture(settings.product_dir, user_id="u1", session_id="LAYUP", filename="a.mp4")
        with mock.patch("motion_agent.app.product_api.record_event"):
            result = finalize_capture(settings, "LAYUP")
        self.assertEqual(result["status"], "no_swing")
        self.assertIn("no_swing", _NOT_COUNTED)
        self.assertIn("running or jumping", result["message"])
        self.assertTrue(result["message"].endswith(NOT_COUNTED_NOTE))
        row = list_captures(settings.product_dir, "u1")[0]
        self.assertEqual(row["detect_message"], result["message"])


@unittest.skipUnless(LAYUP_CAPTURE.is_file(), "layup capture not in this checkout")
class SessionMessageTests(unittest.TestCase):
    """GET /api/session shows the not-golf reason on both the old page and /app."""

    def setUp(self):
        from motion_agent.app import chat_server
        from motion_agent.app.accounts import login, register
        from motion_agent.app.config import Settings
        from motion_agent.app.library import add_capture

        self._tmp = tempfile.TemporaryDirectory()
        data = Path(self._tmp.name) / "data"
        self.settings = Settings(
            data_dir=data,
            host="127.0.0.1",
            port=0,
            backend="mock",
            prompt_password=None,
            max_upload_bytes=1024,
            invite_daily_limit=3,
        )
        _place(data, "LAYUP", LAYUP_CAPTURE)
        user = register(self.settings.product_dir, "a@example.com", "password123", accepted_terms=True)["user"]
        add_capture(self.settings.product_dir, user_id=user["user_id"], session_id="LAYUP", filename="a.mp4")
        self.token = login(self.settings.product_dir, "a@example.com", "password123")["token"]
        for p in (
            mock.patch.object(chat_server, "SETTINGS", self.settings),
            mock.patch.object(chat_server, "DATA_DIR", data),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def _session(self) -> dict:
        url = f"http://127.0.0.1:{self.server.server_address[1]}/api/session?session_id=LAYUP"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": self.token})) as resp:
            return json.loads(resp.read())

    def test_uncounted_upload_shows_the_saved_message(self):
        from motion_agent.app.library import update_status

        saved = "This doesn't look like a golf swing — saved. This upload does not count toward your monthly analyses."
        update_status(self.settings.product_dir, "LAYUP", "no_swing", detect_message=saved)
        body = self._session()
        self.assertTrue(body["no_swing"])
        self.assertEqual(body["sport"], "general")
        self.assertEqual(body["detect_message"], saved)

    def test_older_counted_upload_gets_the_reason_without_the_quota_line(self):
        from motion_agent.app.library import update_status
        from motion_agent.app.product_api import NOT_COUNTED_NOTE

        update_status(self.settings.product_dir, "LAYUP", "ready", detect_message="Detected Golf.")
        body = self._session()
        self.assertTrue(body["no_swing"])
        self.assertIn("running or jumping", body["detect_message"])
        self.assertNotIn(NOT_COUNTED_NOTE, body["detect_message"])


class MessageTests(unittest.TestCase):
    def test_every_rule_and_look_has_copy(self):
        for check in golf_gate.CHECK_ORDER:
            if check in golf_gate.NO_SWING_CHECKS:
                continue
            self.assertIn(check, golf_gate.REASON_PHRASES)
            self.assertTrue(golf_gate.gate_message(check, None).startswith("This doesn't look like a golf swing"))
        for look, phrase in golf_gate.LOOKS_LIKE_PHRASES.items():
            self.assertIn(phrase, golf_gate.gate_message("two_hands", look))
        self.assertEqual(golf_gate.gate_message(None, None), "")


if __name__ == "__main__":
    unittest.main()
