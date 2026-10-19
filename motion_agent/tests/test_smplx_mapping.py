"""Tests for SMPL-X joint mapping."""

from __future__ import annotations

import unittest

from motion_agent.motion.smplx_mapping import (
    SMPL_X_JOINT_NAMES,
    joint_idx,
    joint_metadata,
    joint_name,
    resolve_joint_name,
)


class TestSmplxMapping(unittest.TestCase):
    def test_roundtrip_indices(self) -> None:
        for i, name in enumerate(SMPL_X_JOINT_NAMES):
            self.assertEqual(joint_idx(name), i)
            self.assertEqual(joint_name(i), name)

    def test_aliases(self) -> None:
        self.assertEqual(resolve_joint_name("knee_r"), "right_knee")
        self.assertEqual(resolve_joint_name("r_knee"), "right_knee")
        self.assertEqual(resolve_joint_name("Right-Knee"), "right_knee")
        self.assertEqual(joint_metadata("hip_l").dynamics_id, "hip_l")
        self.assertEqual(joint_metadata("right_knee").primary_angle_coord, "knee_angle_r")

    def test_unknown(self) -> None:
        with self.assertRaises(KeyError):
            resolve_joint_name("not_a_joint")

    def test_hand_chain_present(self) -> None:
        self.assertIn("left_index1", SMPL_X_JOINT_NAMES)
        self.assertIn("right_thumb3", SMPL_X_JOINT_NAMES)


if __name__ == "__main__":
    unittest.main()
