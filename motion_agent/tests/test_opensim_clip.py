"""Tests for OpenSim skeleton phase clips (Kinetic Lens path)."""

from __future__ import annotations

import unittest
from pathlib import Path

from motion_agent.mesh.opensim_clip import (
    get_opensim_skeleton_clip,
    opensim_skeleton_status,
)

DATA_DIR = Path("/root/CerebelServer/data")
SESSION = DATA_DIR / "sessions" / "2151D83E-CF3E-4680-8047-B5F8C209D14B"


class TestOpenSimSkeletonClip(unittest.TestCase):
    def test_status_and_clip(self) -> None:
        if not SESSION.is_dir():
            self.skipTest("sample session missing")
        status = opensim_skeleton_status(SESSION)
        self.assertFalse(status.get("error"))
        self.assertTrue(status.get("motion_available"))
        self.assertTrue(status.get("glb_available"))
        clip = get_opensim_skeleton_clip(SESSION, 5.1, 5.5, include_muscles=True)
        self.assertFalse(clip.get("error"), clip)
        self.assertEqual(clip.get("view"), "opensim")
        self.assertGreaterEqual(clip.get("frame_count", 0), 1)
        self.assertTrue(clip.get("glb_url", "").endswith("rajagopal-bones.glb"))
        self.assertIn("pelvis", clip.get("body_names") or [])
        fr0 = clip["frames"][0]
        self.assertIn("bodies", fr0)
        self.assertIn("p", fr0["bodies"]["pelvis"])
        self.assertIn("q", fr0["bodies"]["pelvis"])
        if clip.get("muscle_count", 0) > 0:
            self.assertTrue(fr0.get("muscles"))
            self.assertGreaterEqual(len(fr0["muscles"][0].get("points") or []), 2)


if __name__ == "__main__":
    unittest.main()
