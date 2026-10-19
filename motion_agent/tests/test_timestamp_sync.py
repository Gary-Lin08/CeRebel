"""Tests for timestamp synchronization."""

from __future__ import annotations

import unittest

import numpy as np

from motion_agent.motion.synchronization import (
    frame_to_timestamp,
    interpolate_series,
    nearest_index,
    slice_indices,
    timestamp_to_frame,
)


class TestTimestampSync(unittest.TestCase):
    def test_frame_timestamp_roundtrip(self) -> None:
        fps = 30.0
        for i in range(0, 100, 7):
            t = frame_to_timestamp(i, fps)
            self.assertEqual(timestamp_to_frame(t, fps, 100), i)

    def test_clamp_frame(self) -> None:
        self.assertEqual(timestamp_to_frame(-1.0, 30.0, 10), 0)
        self.assertEqual(timestamp_to_frame(999.0, 30.0, 10), 9)

    def test_nearest_and_slice(self) -> None:
        ts = np.linspace(0, 10, 101)
        self.assertEqual(nearest_index(ts, 3.33), 33)
        sl = slice_indices(ts, 2.0, 4.0)
        self.assertTrue(np.all(ts[sl] >= 2.0 - 1e-9))
        self.assertTrue(np.all(ts[sl] <= 4.0 + 1e-9))

    def test_interpolate(self) -> None:
        ts = np.array([0.0, 1.0, 2.0])
        vals = np.array([[0.0, 0.0], [2.0, 4.0], [4.0, 8.0]])
        mid = interpolate_series(ts, vals, 0.5)
        np.testing.assert_allclose(mid, [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
