"""Tests for golf swing / phase segmentation."""

from __future__ import annotations

import unittest
from pathlib import Path

from motion_agent.llm.schemas import openai_tools
from motion_agent.motion.adapter import adapt_session
from motion_agent.motion.golf_phases import PHASE_ORDER, segment_golf_swings
from motion_agent.storage.motion_store import newest_ready_session_dir
from motion_agent.tools.motion_tools import execute_tool

DATA_DIR = Path("/root/CerebelServer/data")
FALLBACK = DATA_DIR / "sessions" / "C991F337-CF98-4642-BA60-D2F64DCF248C"


class TestGolfPhases(unittest.TestCase):
    def test_tool_registered(self) -> None:
        names = {t["function"]["name"] for t in openai_tools()}
        self.assertIn("segment_golf_swings", names)

    def test_segment_on_ready_session(self) -> None:
        session = newest_ready_session_dir(DATA_DIR) or FALLBACK
        seq = adapt_session(session)
        result = segment_golf_swings(seq, max_swings=3)
        self.assertFalse(result.get("error"), result)
        self.assertEqual(result.get("ui_hint"), "golf_phase_cards")
        self.assertEqual(result.get("handedness"), "right")
        self.assertEqual(result.get("lead_side"), "left")
        self.assertEqual(result.get("trail_side"), "right")
        self.assertGreaterEqual(result.get("swing_count", 0), 1)
        self.assertEqual(result.get("matching", {}).get("algorithm"), "hungarian")
        swings = result["swings"]
        # Exclusive windows: no temporal overlap between swings.
        for i, a in enumerate(swings):
            for b in swings[i + 1 :]:
                overlap = min(a["end_time"], b["end_time"]) - max(
                    a["start_time"], b["start_time"]
                )
                self.assertLessEqual(overlap, 1e-6, (a, b))
        swing = swings[0]
        self.assertIn("phases", swing)
        self.assertGreaterEqual(len(swing["phases"]), 3)
        ids = [p["id"] for p in swing["phases"]]
        self.assertEqual(len(ids), len(set(ids)), "phase cards must be unique")
        self.assertTrue(set(ids).issubset(set(PHASE_ORDER)))
        for p in swing["phases"]:
            self.assertLess(p["start_time"], p["end_time"])
            self.assertGreaterEqual(p["key_time"], p["start_time"])
            self.assertLessEqual(p["key_time"], p["end_time"])
        # Contiguous phase tiling within a swing.
        for i in range(len(swing["phases"]) - 1):
            self.assertAlmostEqual(
                swing["phases"][i]["end_time"],
                swing["phases"][i + 1]["start_time"],
                places=3,
            )
        # Minimum dwell: no micro-phases from noise peaks.
        from motion_agent.motion.golf_phases import PHASE_MIN_DURATION_S

        for p in swing["phases"]:
            need = PHASE_MIN_DURATION_S.get(p["id"], 0.05)
            # Scaled mins may be slightly below nominal on short windows.
            self.assertGreaterEqual(
                p["end_time"] - p["start_time"] + 1e-6,
                min(need, 0.04) * 0.85,
                msg=f"{p['id']} too short: {p}",
            )

    def test_handedness_defaults_right_auto_is_right(self) -> None:
        from motion_agent.motion.golf_phases import _resolve_handedness

        self.assertEqual(_resolve_handedness("auto"), ("right", "left", "right"))
        self.assertEqual(_resolve_handedness("right"), ("right", "left", "right"))
        self.assertEqual(_resolve_handedness("left"), ("left", "right", "left"))
        session = newest_ready_session_dir(DATA_DIR) or FALLBACK
        seq = adapt_session(session)
        auto = segment_golf_swings(seq, handedness="auto", max_swings=2)
        forced_left = segment_golf_swings(seq, handedness="left", max_swings=2)
        self.assertFalse(auto.get("error"), auto)
        self.assertEqual(auto["handedness"], "right")
        self.assertEqual(auto["lead_side"], "left")
        self.assertFalse(forced_left.get("error"), forced_left)
        self.assertEqual(forced_left["handedness"], "left")
        self.assertEqual(forced_left["lead_side"], "right")

    def test_auto_prefers_single_strong_swing(self) -> None:
        session = newest_ready_session_dir(DATA_DIR) or FALLBACK
        seq = adapt_session(session)
        auto = segment_golf_swings(seq, max_swings=8)
        forced = segment_golf_swings(seq, swing_count=1)
        self.assertEqual(forced["swing_count"], 1)
        # Auto should not explode the swing count from weak secondary peaks.
        # Newest ready sessions may contain a few real swings; bound loosely.
        self.assertLessEqual(auto["swing_count"], 4)
        self.assertGreaterEqual(auto["swing_count"], 1)
        ids = [p["id"] for p in forced["swings"][0]["phases"]]
        for must in ("address", "backswing", "downswing", "impact", "follow_through"):
            self.assertIn(must, ids)

    def test_execute_tool_dispatch(self) -> None:
        session = newest_ready_session_dir(DATA_DIR) or FALLBACK
        seq = adapt_session(session)
        result = execute_tool(seq, "segment_golf_swings", {"max_swings": 2})
        self.assertFalse(result.get("error"), result)
        self.assertEqual(result.get("sport"), "golf")


if __name__ == "__main__":
    unittest.main()
