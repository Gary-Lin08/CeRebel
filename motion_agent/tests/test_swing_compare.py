"""Progress view: compare two of a player's own swings."""

from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import numpy as np

from motion_agent.app.config import Settings
from motion_agent.app.library import add_capture, delete_capture, update_status
from motion_agent.motion.adapter import adapt_session
from motion_agent.motion.camera_view import estimate_view
from motion_agent.motion.swing_compare import (
    METRIC_BY_ID,
    METRICS,
    compare_snapshots,
    goal_met,
    swing_snapshot,
)

REPO = Path(__file__).resolve().parents[2]
SAMPLES = REPO / "kinetic-lens" / "retargeting_neck_v2" / "outputs"
SAMPLE_A = SAMPLES / "1872A5C0-60F0-4C3B-9A1E-000000060001" / "new" / "opensim-motion.json"
SAMPLE_B = SAMPLES / "3F7E02D7-706E-4916-8F75-E0439FFFCE7B" / "new" / "opensim-motion.json"
INTERNAL_NAMES = re.compile(r"opensim|gem-?x|soma|rajagopal|smpl", re.IGNORECASE)


def _need_samples(test: unittest.TestCase) -> None:
    if not (SAMPLE_A.is_file() and SAMPLE_B.is_file()):
        test.skipTest("sample swings not checked out")


def _place_session(data_dir: Path, session_id: str, motion: Path) -> Path:
    ik = data_dir / "sessions" / session_id / "ik"
    ik.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(motion, ik / "opensim-motion.json")
    return ik.parent


# ---------------------------------------------------------------- camera direction


def _pose3d(azimuth_deg: float, *, frames: int = 12, centred: bool = False) -> dict:
    """A golfer at address (target line along Z) seen by a level camera at the given azimuth."""
    world = np.zeros((33, 3))
    world[0] = (0.05, 1.65, 0.0)
    world[11], world[12] = (0.05, 1.40, -0.20), (0.05, 1.40, 0.20)
    world[23], world[24] = (0.0, 0.95, -0.15), (0.0, 0.95, 0.15)
    world[25], world[26] = (0.05, 0.50, -0.17), (0.05, 0.50, 0.17)
    world[27], world[28] = (0.0, 0.08, -0.20), (0.0, 0.08, 0.20)
    phi = math.radians(azimuth_deg)
    right = np.array([math.sin(phi), 0.0, math.cos(phi)])
    width, height, px_per_m = 1080.0, 1920.0, 600.0
    image = np.zeros((33, 3))
    image[:, 0] = (540.0 + px_per_m * world @ right) / width
    image[:, 1] = (1500.0 - px_per_m * world[:, 1]) / height
    if centred:
        image[:, :2] = 0.5
    return {
        "metadata": {"imageSize": [width, height]},
        "frames": [
            {"time": i / 30.0, "image": image.tolist(), "world": world.tolist()} for i in range(frames)
        ],
    }


class CameraViewTests(unittest.TestCase):
    def test_recovers_azimuth_and_label(self):
        for azimuth, label in ((0.0, "face_on"), (45.0, "angled"), (85.0, "down_the_line")):
            view = estimate_view(_pose3d(azimuth), 0.0, 0.4)
            self.assertTrue(view["known"])
            self.assertEqual(view["id"], label)
            self.assertAlmostEqual(view["azimuth_deg"], azimuth, delta=3.0)

    def test_unknown_without_landmarks_or_camera(self):
        self.assertFalse(estimate_view(None, 0.0, 0.4)["known"])
        self.assertFalse(estimate_view({"frames": []}, 0.0, 0.4)["known"])
        # Exporter wrote no camera projection: every image point sits at the centre.
        self.assertFalse(estimate_view(_pose3d(0.0, centred=True), 0.0, 0.4)["known"])


# ---------------------------------------------------------------- judging (synthetic swings)


GOALS = {
    "separation_top": {"min": 20.0, "label": "20°+"},
    "trail_knee_loss": {"max": 8.0, "label": "under 8°"},
    "lead_elbow_top": {"max": 30.0, "label": "under 30°"},
    "pelvis_drift_after_impact": {"max": 3.5, "label": "under 3.5 cm"},
}
BASE_VALUES = {
    "separation_top": 30.0,
    "trail_knee_loss": 4.0,
    "lead_elbow_top": 20.0,
    "pelvis_drift_after_impact": 2.0,
    "trail_hand_peak": 7.0,
    "tempo": 3.0,
}


def _snap(*, findings=(), fps=30.0, azimuth=None, alignment="good", **values):
    vals = dict(BASE_VALUES)
    vals.update(values)
    view = {"known": False} if azimuth is None else {"known": True, "azimuth_deg": azimuth}
    times = {"address": 0.6, "top": 1.5, "impact": 1.8, "finish": 2.4, "hand_peak": 1.79}
    return {
        "ok": True,
        "values": vals,
        "goals": GOALS,
        "fired": list(findings),
        "findings": [{"id": f, "headline": f"Headline {f}"} for f in findings],
        "view": view,
        "fps": fps,
        "alignment": alignment,
        "missing": [],
        "events": {k: v for k, v in times.items() if k != "hand_peak"},
        "metric_times": times,
        "series": [],
        "start_time": 0.0,
        "end_time": 2.5,
    }


class JudgingTests(unittest.TestCase):
    def _metric(self, result, mid):
        return next(m for m in result["metrics"] if m["id"] == mid)

    def test_main_issue_states(self):
        cases = [
            # (before, after, still fires after, expected status)
            (12.0, 24.0, False, "resolved"),
            (18.0, 21.0, False, "borderline"),
            (5.0, 12.0, True, "improving"),
            (12.0, 14.0, True, "unchanged"),
            (15.0, 8.0, True, "worse"),
        ]
        for before, after, fires, status in cases:
            with self.subTest(before=before, after=after):
                result = compare_snapshots(
                    _snap(findings=["separation"], separation_top=before),
                    _snap(findings=["separation"] if fires else [], separation_top=after),
                )
                issue = result["main_issue"]
                self.assertEqual(issue["finding_id"], "separation")
                self.assertEqual(issue["metric_id"], "separation_top")
                self.assertEqual(issue["status"], status)
                self.assertIn(issue["display"], issue["message"])

    def test_noise_floor_boundary(self):
        floor = METRIC_BY_ID["trail_knee_loss"].noise_floor
        just_under = compare_snapshots(_snap(trail_knee_loss=10.0), _snap(trail_knee_loss=10.0 - floor + 0.01))
        at_floor = compare_snapshots(_snap(trail_knee_loss=10.0), _snap(trail_knee_loss=10.0 - floor))
        self.assertEqual(self._metric(just_under, "trail_knee_loss")["verdict"], "no_clear_change")
        self.assertEqual(self._metric(at_floor, "trail_knee_loss")["verdict"], "improved")
        worse = compare_snapshots(_snap(lead_elbow_top=20.0), _snap(lead_elbow_top=30.0))
        self.assertEqual(self._metric(worse, "lead_elbow_top")["verdict"], "worse")

    def test_shown_only_metrics_are_never_judged(self):
        result = compare_snapshots(_snap(trail_hand_peak=6.0, tempo=2.0), _snap(trail_hand_peak=9.0, tempo=3.5))
        self.assertEqual(self._metric(result, "trail_hand_peak")["verdict"], "info")
        self.assertEqual(self._metric(result, "tempo")["verdict"], "info")

    def test_every_number_is_marked_as_estimate_with_margin(self):
        result = compare_snapshots(_snap(), _snap())
        self.assertEqual(len(result["metrics"]), len(METRICS))
        for metric in result["metrics"]:
            self.assertTrue(metric["estimate"])
            self.assertEqual(metric["noise_floor_source"], "provisional")
            self.assertTrue(metric["margin"].startswith("±"))
        self.assertEqual(self._metric(result, "tempo")["margin"], "±0.3")
        self.assertEqual(self._metric(result, "separation_top")["margin"], "±5°")
        self.assertIn("single phone camera", result["disclosure"])

    def test_different_camera_angle_blocks_view_sensitive_metrics(self):
        result = compare_snapshots(
            _snap(findings=["separation"], separation_top=10.0, azimuth=10.0),
            _snap(separation_top=40.0, trail_knee_loss=12.0, azimuth=80.0),
        )
        self.assertEqual(result["comparability"]["level"], "limited")
        self.assertIn("camera_angle_differs", result["comparability"]["reasons"])
        self.assertFalse(result["comparability"]["same_camera_angle"])
        self.assertEqual(self._metric(result, "separation_top")["verdict"], "not_comparable")
        self.assertEqual(self._metric(result, "pelvis_drift_after_impact")["verdict"], "not_comparable")
        self.assertEqual(self._metric(result, "trail_knee_loss")["verdict"], "worse")
        self.assertEqual(result["main_issue"]["status"], "not_comparable")
        self.assertEqual(result["main_issue"]["reason"], "camera_angle_differs")

    def test_same_camera_angle_within_tolerance(self):
        result = compare_snapshots(_snap(azimuth=70.0), _snap(azimuth=85.0))
        self.assertTrue(result["comparability"]["same_camera_angle"])
        self.assertEqual(result["comparability"]["level"], "full")
        self.assertEqual(result["comparability"]["message"], "")

    def test_unknown_angle_still_judges_with_a_tip(self):
        result = compare_snapshots(_snap(separation_top=10.0), _snap(separation_top=30.0))
        self.assertIsNone(result["comparability"]["same_camera_angle"])
        self.assertEqual(self._metric(result, "separation_top")["verdict"], "improved")
        self.assertIn("same spot", result["comparability"]["message"])

    def test_frame_rate_gap_blocks_speed_and_tempo(self):
        result = compare_snapshots(_snap(fps=30.0), _snap(fps=120.0, trail_hand_peak=9.0))
        self.assertIn("frame_rate_differs", result["comparability"]["reasons"])
        self.assertEqual(self._metric(result, "trail_hand_peak")["verdict"], "not_comparable")
        self.assertEqual(self._metric(result, "tempo")["verdict"], "not_comparable")
        self.assertEqual(self._metric(result, "separation_top")["verdict"], "no_clear_change")

    def test_unclear_phases_mean_nothing_to_line_up(self):
        result = compare_snapshots(_snap(findings=["trail-knee"]), _snap(alignment="none"))
        self.assertEqual(result["comparability"]["level"], "none")
        self.assertEqual(result["metrics"], [])
        self.assertEqual(result["main_issue"]["status"], "not_comparable")
        self.assertEqual(result["main_issue"]["reason"], "phases_unclear")
        self.assertIsNone(result["overlay"])

    def test_no_real_issue_last_time(self):
        result = compare_snapshots(_snap(), _snap(findings=["lead-arm"]))
        self.assertIsNone(result["main_issue"])
        self.assertEqual(result["next_issue"]["finding_id"], "lead-arm")
        self.assertFalse(result["next_issue"]["same_as_before"])


# ---------------------------------------------------------------- real swings


class RealSwingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (SAMPLE_A.is_file() and SAMPLE_B.is_file()):
            raise unittest.SkipTest("sample swings not checked out")
        cls._tmp = tempfile.TemporaryDirectory()
        data = Path(cls._tmp.name)
        cls.a = swing_snapshot(adapt_session(_place_session(data, "A", SAMPLE_A)))
        cls.b = swing_snapshot(adapt_session(_place_session(data, "B", SAMPLE_B)))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_snapshot_has_ordered_events_and_every_metric(self):
        for snap in (self.a, self.b):
            self.assertTrue(snap["ok"])
            self.assertEqual(snap["alignment"], "good")
            ev = snap["events"]
            self.assertLess(ev["address"], ev["top"])
            self.assertLess(ev["top"], ev["impact"])
            self.assertLess(ev["impact"], ev["finish"])
            for spec in METRICS:
                self.assertIsNotNone(snap["values"][spec.id], spec.id)

    def test_goal_lines_match_the_analysis_rules(self):
        """A metric on the good side of its goal line is exactly a fix the analysis did not raise."""
        for snap in (self.a, self.b):
            for spec in METRICS:
                if not spec.finding:
                    continue
                met = goal_met(spec, snap)
                if met is None:
                    continue
                self.assertEqual(met, spec.finding not in snap["fired"], spec.id)

    def test_swing_against_itself_shows_no_change(self):
        result = compare_snapshots(self.a, self.a)
        self.assertEqual(result["comparability"]["level"], "full")
        for metric in result["metrics"]:
            self.assertIn(metric["verdict"], ("no_clear_change", "info"))
        self.assertEqual(result["main_issue"]["status"], "unchanged")

    def test_two_swings_line_up_on_one_phase_axis(self):
        result = compare_snapshots(self.a, self.b)
        self.assertEqual(result["alignment"]["quality"], "good")
        self.assertEqual(result["main_issue"]["finding_id"], self.a["fired"][0])
        overlay = result["overlay"]
        self.assertEqual([m["id"] for m in overlay["marks"]], ["address", "top", "impact", "finish"])
        self.assertGreaterEqual(len(overlay["series"]), 4)
        for series in overlay["series"]:
            self.assertEqual(len(series["before"]), len(overlay["u"]))
            self.assertEqual(len(series["after"]), len(overlay["u"]))
        sep = next(s for s in overlay["series"] if s["id"] == "separation")
        top_i = int(round(0.55 * (len(overlay["u"]) - 1)))
        self.assertGreater(sep["before"][top_i], 0)
        self.assertGreater(sep["after"][top_i], 0)
        for metric in result["metrics"]:
            self.assertIsNotNone(metric["time_before"])
            self.assertIsNotNone(metric["time_after"])


# ---------------------------------------------------------------- permissions and tiers


def _settings(data: Path) -> Settings:
    return Settings(
        data_dir=data,
        host="127.0.0.1",
        port=1,
        backend="mock",
        prompt_password=None,
        max_upload_bytes=1024,
        invite_daily_limit=3,
    )


class CompareAccessTests(unittest.TestCase):
    """u1 owns S1 < S2 < S3 (ready), N1 (no swing, between S2 and S3), P1 (processing); u2 owns X1."""

    def setUp(self):
        _need_samples(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        self.settings = _settings(self.data)
        product = self.settings.product_dir
        rows = [
            ("u1", "S1", SAMPLE_A, "ready", "2026-10-01T10:00:00+00:00"),
            ("u1", "S2", SAMPLE_B, "ready", "2026-10-03T10:00:00+00:00"),
            ("u1", "N1", SAMPLE_B, "no_swing", "2026-10-04T10:00:00+00:00"),
            ("u1", "S3", SAMPLE_A, "ready", "2026-10-05T10:00:00+00:00"),
            ("u1", "P1", None, "gemx", "2026-10-06T10:00:00+00:00"),
            ("u2", "X1", SAMPLE_A, "ready", "2026-10-02T10:00:00+00:00"),
        ]
        for user, sid, motion, status, created in rows:
            if motion is not None:
                session = _place_session(self.data, sid, motion)
                # finalize_capture records the outcome here; the library re-reads it.
                (session / "upload_progress.json").write_text(json.dumps({"stage": status}), encoding="utf-8")
            add_capture(product, user_id=user, session_id=sid, filename=f"{sid.lower()}.mp4")
            update_status(product, sid, status, created_at=created)
        self.u1 = {"user_id": "u1", "email": "one@example.com"}

    def tearDown(self):
        self._tmp.cleanup()

    def _compare(self, before, after, user=None, **kw):
        from motion_agent.app.compare import compare_payload

        return compare_payload(self.settings, user or self.u1, before, after, **kw)

    def test_adjacent_pair_is_free(self):
        code, payload = self._compare("S1", "S2")
        self.assertEqual(code, 200, payload)
        self.assertTrue(payload["adjacent"])
        self.assertFalse(payload["reordered"])
        self.assertEqual(payload["before"]["session_id"], "S1")
        self.assertEqual(payload["after"]["session_id"], "S2")
        self.assertTrue(payload["metrics"])
        self.assertIn(payload["main_issue"]["status"], ("resolved", "borderline", "improving", "unchanged", "worse"))
        for side in ("before", "after"):
            mark = payload[side]["highlight"]
            self.assertEqual(mark["finding_id"], payload["main_issue"]["finding_id"])
            self.assertTrue(mark["overlay"]["segments"])
            self.assertLess(mark["start_time"], mark["end_time"])

    def test_reversed_request_is_put_in_time_order(self):
        code, payload = self._compare("S2", "S1")
        self.assertEqual(code, 200)
        self.assertTrue(payload["reordered"])
        self.assertEqual(payload["before"]["session_id"], "S1")

    def test_unmeasurable_capture_in_between_does_not_break_adjacency(self):
        code, payload = self._compare("S2", "S3")
        self.assertEqual(code, 200, payload)
        self.assertTrue(payload["adjacent"])

    def test_any_pair_needs_pro(self):
        code, payload = self._compare("S1", "S3")
        self.assertEqual(code, 402)
        self.assertTrue(payload["upgrade"])
        self.assertEqual(payload["default"], {"before": "S2", "after": "S3"})
        with mock.patch("motion_agent.app.billing.is_pro", return_value=True):
            code, payload = self._compare("S1", "S3")
        self.assertEqual(code, 200)
        self.assertFalse(payload["adjacent"])
        self.assertTrue(payload["any_pair"])

    def test_only_own_captures(self):
        self.assertEqual(self._compare("S1", "X1")[0], 403)
        self.assertEqual(self._compare("X1", "S1")[0], 403)
        self.assertEqual(self._compare("S1", "NOPE")[0], 403)
        other = {"user_id": "u2", "email": "two@example.com"}
        self.assertEqual(self._compare("S1", "S2", user=other)[0], 403)
        delete_capture(self.settings.product_dir, "u1", "S2")
        self.assertEqual(self._compare("S1", "S2")[0], 403)

    def test_bad_requests(self):
        self.assertEqual(self._compare("S1", "S1")[0], 400)
        self.assertEqual(self._compare("", "S1")[0], 400)
        self.assertEqual(self._compare("S1", "P1")[0], 409)
        self.assertEqual(self._compare("S2", "N1")[0], 422)

    def test_media_reflects_retention(self):
        (self.data / "sessions" / "S1" / "ik" / "session.mp4").write_bytes(b"\x00")
        (self.data / "sessions" / "S2" / "retention.json").write_text("{}", encoding="utf-8")
        code, payload = self._compare("S1", "S2")
        self.assertEqual(code, 200)
        self.assertEqual(payload["before"]["media"], {"video_url": "/media/S1/session.mp4", "video_removed": False})
        self.assertEqual(payload["after"]["media"], {"video_url": None, "video_removed": True})

    def test_no_internal_names_in_payload(self):
        code, payload = self._compare("S1", "S2")
        self.assertEqual(code, 200)
        self.assertIsNone(INTERNAL_NAMES.search(json.dumps(payload)))

    def test_view_is_recorded_without_content(self):
        self._compare("S1", "S2")
        rows = [
            json.loads(line)
            for line in (self.settings.product_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        event = next(r for r in rows if r["kind"] == "compare_view")
        self.assertEqual((event["user_id"], event["before"], event["after"]), ("u1", "S1", "S2"))

    def test_library_offers_the_default_pair(self):
        from motion_agent.app.product_api import library_payload

        payload = library_payload(self.settings, self.u1)
        self.assertEqual(payload["compare"], {"default": {"before": "S2", "after": "S3"}, "any_pair": False})


class CompareHttpTests(unittest.TestCase):
    def setUp(self):
        _need_samples(self)
        from motion_agent.app import chat_server
        from motion_agent.app.accounts import register

        self._tmp = tempfile.TemporaryDirectory()
        data = Path(self._tmp.name) / "data"
        settings = _settings(data)
        created = register(settings.product_dir, "golfer@example.com", "swing-fix-123", accepted_terms=True)
        self.token = created["token"]
        uid = created["user"]["user_id"]
        for sid, motion, created_at in (("H1", SAMPLE_A, "2026-10-01T10:00:00+00:00"), ("H2", SAMPLE_B, "2026-10-02T10:00:00+00:00")):
            _place_session(data, sid, motion)
            add_capture(settings.product_dir, user_id=uid, session_id=sid, filename=f"{sid}.mp4")
            update_status(settings.product_dir, sid, "ready", created_at=created_at)
        (data / "sessions" / "H1" / "ik" / "session.mp4").write_bytes(b"\x00")
        self._patches = [
            mock.patch.object(chat_server, "SETTINGS", settings),
            mock.patch.object(chat_server, "DATA_DIR", data),
        ]
        for patch in self._patches:
            patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def _get(self, path, token=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_address[1]}{path}")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_sign_in_required(self):
        code, _ = self._get("/api/compare?before=H1&after=H2")
        self.assertEqual(code, 401)

    def test_compare_over_http_signs_video(self):
        code, payload = self._get("/api/compare?before=H1&after=H2", token=self.token)
        self.assertEqual(code, 200, payload)
        url = payload["before"]["media"]["video_url"]
        self.assertTrue(url.startswith("/media/H1/session.mp4?"), url)
        self.assertIsNone(payload["after"]["media"]["video_url"])
        code, library = self._get("/api/library", token=self.token)
        self.assertEqual(code, 200)
        self.assertEqual(library["compare"]["default"], {"before": "H1", "after": "H2"})


if __name__ == "__main__":
    unittest.main()
