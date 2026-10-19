"""Unit tests that do not depend on a live OpenSim IK solve."""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from retargeting_neck_v2.anatomical_landmarks import LANDMARK_NAMES, default_offsets
from retargeting_neck_v2.build_augmented_model import build_augmented_model
from retargeting_neck_v2.checks import check_model
from retargeting_neck_v2.config import HEAD_MARKER_LOCAL, LEGACY_MARKER_SPECS, MARKER_WEIGHTS
from retargeting_neck_v2.generate_markers import head_markers_collinear
from retargeting_neck_v2.rotations import geodesic_deg, rodrigues


class ModelTests(unittest.TestCase):
    def test_augmented_model_topology(self) -> None:
        dest = Path(__file__).resolve().parent.parent / "models" / "RajagopalLaiUhlrich2023_neck.osim"
        build_augmented_model(dest=dest)
        check_model(dest)


class MarkerTests(unittest.TestCase):
    def test_legacy_markers_preserved(self) -> None:
        for name in (
            "L_HIP",
            "R_HIP",
            "L_SHOULDER",
            "R_SHOULDER",
            "L_KNEE",
            "R_KNEE",
            "L_ANKLE",
            "R_ANKLE",
            "L_TOE",
            "R_TOE",
            "L_ELBOW",
            "R_ELBOW",
            "L_WRIST",
            "R_WRIST",
        ):
            self.assertIn(name, LEGACY_MARKER_SPECS)
            self.assertIn(name, MARKER_WEIGHTS)

    def test_neck_markers_named_and_weighted(self) -> None:
        for name in LANDMARK_NAMES:
            self.assertIn(name, MARKER_WEIGHTS)

    def test_head_offsets_noncollinear(self) -> None:
        a = np.array(HEAD_MARKER_LOCAL["HEAD_A"])
        b = np.array(HEAD_MARKER_LOCAL["HEAD_B"])
        c = np.array(HEAD_MARKER_LOCAL["HEAD_C"])
        area = float(np.linalg.norm(np.cross(b - a, c - a)))
        self.assertGreater(area, 1e-4)
        fake = [{"HEAD_A": a, "HEAD_B": b, "HEAD_C": c}]
        self.assertFalse(head_markers_collinear(fake))

    def test_offsets_are_explicit_data(self) -> None:
        data = default_offsets()
        self.assertIn("delta_m", data)
        self.assertEqual(set(data["delta_m"]), set(LANDMARK_NAMES))


class RotationTests(unittest.TestCase):
    def test_identity_geodesic(self) -> None:
        r = rodrigues(np.zeros(3))
        self.assertLess(geodesic_deg(r, np.eye(3)), 1e-6)

    def test_known_angle(self) -> None:
        r = rodrigues(np.array([0.0, np.deg2rad(30.0), 0.0]))
        self.assertAlmostEqual(geodesic_deg(np.eye(3), r), 30.0, places=4)


if __name__ == "__main__":
    unittest.main()
