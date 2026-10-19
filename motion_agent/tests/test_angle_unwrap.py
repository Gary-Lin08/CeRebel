"""Tests for angle unwrapping and continuous root orientation."""

from __future__ import annotations

import unittest

import numpy as np

from motion_agent.motion.adapter import adapt_session
from motion_agent.motion.angles import (
    continuous_euler_xyz_from_quaternions,
    intrinsic_somersault_from_root,
    somersault_direction_label,
    unwrap_radians,
    wrap_risk_metadata,
)
from motion_agent.storage.motion_store import newest_ready_session_dir
from motion_agent.tools.motion_tools import execute_tool


class TestAngleUnwrap(unittest.TestCase):
    def test_synthetic_wrap(self) -> None:
        # Series that jumps across -π → +π
        wrapped = np.array([0.0, -1.0, -2.0, -3.0, 3.0, 2.0], dtype=np.float64)
        uw = unwrap_radians(wrapped)
        self.assertTrue(np.all(np.diff(uw) < 0))  # monotonically decreasing
        self.assertLess(uw[-1], -3.0)

    def test_quaternion_euler_continuous(self) -> None:
        # Rotate about X through >180° via successive quaternions
        angles = np.linspace(0, np.deg2rad(400), 41)
        # xyzw for rotation about X
        quats = np.column_stack(
            [
                np.sin(angles / 2),
                np.zeros_like(angles),
                np.zeros_like(angles),
                np.cos(angles / 2),
            ]
        )
        eul = continuous_euler_xyz_from_quaternions(quats)
        self.assertGreater(float(eul[-1, 0] - eul[0, 0]), np.deg2rad(350))

    def test_wrap_risk_near_pi(self) -> None:
        risk = wrap_risk_metadata(np.deg2rad(-179.0), np.deg2rad(-220.0))
        self.assertTrue(risk["near_singularity_or_wrap"])

    def test_synthetic_backflip_somersault_positive(self) -> None:
        # Face +X; backflip is +ω about rightward (+Z = facing × up)
        n = 61
        dt = 1.0 / 30.0
        angles = np.linspace(0, np.deg2rad(360), n)
        quats = np.column_stack(
            [
                np.zeros(n),
                np.zeros(n),
                np.sin(angles / 2),
                np.cos(angles / 2),
            ]
        )
        com = np.column_stack([np.linspace(0, 10, n), np.ones(n), np.zeros(n)])
        soma = intrinsic_somersault_from_root(quats, com, dt)
        net = float(soma["somersault_cumulative_rad"][-1])
        self.assertGreater(net, np.deg2rad(300))
        self.assertEqual(somersault_direction_label(net), "backflip_direction")


class TestSessionOrientation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        session = newest_ready_session_dir()
        cls.seq = adapt_session(session)

    def test_motion_state_exposes_continuous_root(self) -> None:
        # Pick a mid-session time; for the flip session prefer ~16s if long enough
        t = 16.0 if self.seq.duration_s > 16.5 else min(3.0, self.seq.duration_s * 0.5)
        out = execute_tool(self.seq, "get_motion_state", {"timestamp": t, "joints": ["pelvis"]})
        self.assertFalse(out.get("error"), out)
        pelvis = out["joints"]["pelvis"]
        self.assertIn("root_euler_continuous_deg", pelvis)
        self.assertIn("root_cumulative_rotation_deg", pelvis)
        self.assertIn("intrinsic_somersault_cumulative_deg", pelvis)
        self.assertEqual(out["root"]["frame"], "world")
        self.assertIn("rider_intrinsic", out["root"])
        self.assertEqual(out["root"]["rider_intrinsic"]["frame"], "rider_intrinsic")

    def test_pelvis_trajectory_includes_root_euler(self) -> None:
        if self.seq.duration_s < 17.0:
            self.skipTest("session too short for flip window")
        out = execute_tool(
            self.seq,
            "get_joint_trajectory",
            {"joint": "pelvis", "start_time": 15.5, "end_time": 16.8},
        )
        self.assertFalse(out.get("error"), out)
        self.assertIn("root_euler_x_continuous_deg", out["series"])
        self.assertIn("intrinsic_somersault_cumulative_deg", out["series"])
        net = out["statistics"]["root_euler_x_net_change_deg"]
        # Full flip should show large continuous Euler change (much larger than DOF stall)
        self.assertGreater(abs(net), 200.0)
        soma_net = out["statistics"]["intrinsic_somersault_net_change_deg"]
        self.assertGreater(abs(soma_net), 200.0)
        self.assertEqual(out["statistics"]["flip_direction_hint"], "backflip_direction")
        self.assertGreater(soma_net, 0.0)


if __name__ == "__main__":
    unittest.main()
