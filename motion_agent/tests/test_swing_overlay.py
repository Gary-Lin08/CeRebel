"""Skeleton lines over the player's video, and how each fix is marked on them."""

from __future__ import annotations

import json
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
from motion_agent.app.library import add_capture, update_status
from motion_agent.motion.adapter import adapt_session
from motion_agent.motion.sport_analysis import build_sport_analysis
from motion_agent.motion.swing_overlay import KEYPOINT_NAMES, SCALE, keypoints_payload, overlay_for_finding

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "kinetic-lens" / "retargeting_neck_v2" / "outputs" / "1872A5C0-60F0-4C3B-9A1E-000000060001" / "new" / "opensim-motion.json"


def _pose3d(frames: int = 6, *, centred: bool = False) -> dict:
    rng = np.random.default_rng(0)
    out = []
    for i in range(frames):
        image = rng.uniform(0.1, 0.9, size=(33, 3))
        if centred:
            image[:, :2] = 0.5
        out.append({"time": i / 30.0, "image": image.tolist(), "world": np.zeros((33, 3)).tolist()})
    return {"metadata": {"fps": 30.0, "imageSize": [720, 1280]}, "frames": out}


class KeypointsTests(unittest.TestCase):
    def test_frames_carry_every_joint_in_frame_units(self):
        pose = _pose3d()
        payload = keypoints_payload(pose)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["image_size"], [720, 1280])
        self.assertEqual(len(payload["frames"]), 6)
        first = payload["frames"][0]
        self.assertEqual(len(first), 2 * len(KEYPOINT_NAMES))
        nose = pose["frames"][0]["image"][0]
        self.assertEqual(first[:2], [round(nose[0] * SCALE), round(nose[1] * SCALE)])
        self.assertTrue(all(0 <= v <= SCALE for v in first))
        for a, b in payload["bones"]:
            self.assertIn(a, payload["names"])
            self.assertIn(b, payload["names"])

    def test_nothing_to_draw_without_a_camera_projection(self):
        self.assertFalse(keypoints_payload(None)["available"])
        self.assertFalse(keypoints_payload({"frames": []})["available"])
        self.assertEqual(keypoints_payload(_pose3d(centred=True))["reason"], "no_projection")

    def test_broken_frame_is_skipped_not_fatal(self):
        pose = _pose3d()
        pose["frames"][2]["image"][11][0] = float("nan")
        payload = keypoints_payload(pose)
        self.assertTrue(payload["available"])
        self.assertIsNone(payload["frames"][2])


class FixMarkingTests(unittest.TestCase):
    def test_sides_follow_handedness(self):
        righty = overlay_for_finding("trail-knee", "trail-knee", "left", "right")
        lefty = overlay_for_finding("trail-knee", "trail-knee", "right", "left")
        self.assertEqual(righty["segments"], [["right_hip", "right_knee"], ["right_knee", "right_ankle"]])
        self.assertEqual(lefty["arrow"]["at"], "left_knee")
        arm = overlay_for_finding("lead-arm", "lead-elbow", "left", "right")
        self.assertEqual(arm["arrow"]["kind"], "extend")
        self.assertEqual(arm["angle"]["series"], "lead-elbow")

    def test_turn_gets_lines_and_a_number_but_no_arrow(self):
        spec = overlay_for_finding("separation", "separation", "left", "right")
        self.assertNotIn("arrow", spec)
        self.assertEqual({line["role"] for line in spec["lines"]}, {"fix", "reference"})
        self.assertEqual(spec["readout"]["series"], "separation")

    def test_early_extension_tracks_the_hips(self):
        spec = overlay_for_finding("early-extension", "pelvis-drift", "left", "right")
        self.assertEqual(spec["path"]["points"], ["left_hip", "right_hip"])

    def test_plain_readings_light_up_their_body_part(self):
        self.assertEqual(overlay_for_finding("read-trail-hand", "trail-hand", "left", "right")["segments"],
                         [["right_elbow", "right_wrist"]])
        self.assertIsNone(overlay_for_finding("read-unknown", "unknown", "left", "right"))

    def test_every_joint_named_in_a_marking_exists(self):
        names = set(KEYPOINT_NAMES)
        for fid, series in (("trail-knee", None), ("lead-arm", None), ("separation", None),
                            ("early-extension", None), ("back-leg", None), ("front-knee", None)):
            spec = overlay_for_finding(fid, series, "left", "right")
            used = {n for seg in spec.get("segments", []) for n in seg}
            for key in ("angle", "arrow"):
                part = spec.get(key) or {}
                used |= {part.get(k) for k in ("at", "from", "to") if part.get(k)}
            used |= {p for line in spec.get("lines", []) for p in (line["a"], line["b"])}
            used |= set((spec.get("path") or {}).get("points", []))
            self.assertTrue(used <= names, (fid, used - names))

    def test_analysis_findings_carry_their_marking(self):
        if not SAMPLE.is_file():
            self.skipTest("sample swing not checked out")
        with tempfile.TemporaryDirectory() as tmp:
            ik = Path(tmp) / "S" / "ik"
            ik.mkdir(parents=True)
            shutil.copyfile(SAMPLE, ik / "opensim-motion.json")
            result = build_sport_analysis(adapt_session(ik.parent), "golf")
        self.assertTrue(result["findings"])
        for note in result["findings"]:
            self.assertIsNotNone(note["overlay"], note["id"])
            self.assertTrue(note["overlay"]["segments"])


class KeypointsHttpTests(unittest.TestCase):
    def setUp(self):
        from motion_agent.app import chat_server
        from motion_agent.app.accounts import register

        self._tmp = tempfile.TemporaryDirectory()
        data = Path(self._tmp.name) / "data"
        settings = Settings(data_dir=data, host="127.0.0.1", port=1, backend="mock", prompt_password=None,
                            max_upload_bytes=1024, invite_daily_limit=3)
        created = register(settings.product_dir, "lines@example.com", "swing-fix-123", accepted_terms=True)
        self.token = created["token"]
        for sid, owner, pose in (("K1", created["user"]["user_id"], _pose3d()), ("K2", "someone-else", _pose3d()),
                                 ("K3", created["user"]["user_id"], None)):
            ik = data / "sessions" / sid / "ik"
            ik.mkdir(parents=True)
            (ik / "opensim-motion.json").write_text("{}", encoding="utf-8")
            if pose is not None:
                (ik / "pose3d.json").write_text(json.dumps(pose), encoding="utf-8")
            add_capture(settings.product_dir, user_id=owner, session_id=sid, filename=f"{sid}.mp4")
            update_status(settings.product_dir, sid, "ready")
        self._patches = [mock.patch.object(chat_server, "SETTINGS", settings), mock.patch.object(chat_server, "DATA_DIR", data)]
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
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_owner_gets_keypoints(self):
        code, payload = self._get("/api/swing_keypoints?session_id=K1", self.token)
        self.assertEqual(code, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(len(payload["frames"]), 6)

    def test_other_accounts_and_anonymous_are_refused(self):
        self.assertEqual(self._get("/api/swing_keypoints?session_id=K2", self.token)[0], 403)
        self.assertEqual(self._get("/api/swing_keypoints?session_id=K1")[0], 403)

    def test_capture_without_landmarks_says_so(self):
        code, payload = self._get("/api/swing_keypoints?session_id=K3", self.token)
        self.assertEqual(code, 200)
        self.assertFalse(payload["available"])


if __name__ == "__main__":
    unittest.main()
