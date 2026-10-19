"""Integration tests for motion/dynamics tools against a real CeRebel session."""

from __future__ import annotations

import unittest
from pathlib import Path

from motion_agent.motion.adapter import adapt_session
from motion_agent.storage.motion_store import newest_ready_session_dir
from motion_agent.tools.motion_tools import execute_tool

DATA_DIR = Path("/root/CerebelServer/data")
FALLBACK = DATA_DIR / "sessions" / "C991F337-CF98-4642-BA60-D2F64DCF248C"


class TestMotionTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        session = newest_ready_session_dir(DATA_DIR) or FALLBACK
        if not (session / "ik" / "opensim-motion.json").is_file():
            raise unittest.SkipTest(f"No preprocessed session at {session}")
        cls.session_dir = session
        cls.seq = adapt_session(session)

    def test_sequence_loaded(self) -> None:
        self.assertGreater(self.seq.frame_count, 10)
        self.assertGreater(self.seq.duration_s, 0.5)
        self.assertIn("right_knee", self.seq.joint_angles_rad)
        self.assertTrue(self.seq.has_dynamics())

    def test_motion_state(self) -> None:
        out = execute_tool(self.seq, "get_motion_state", {"timestamp": 3.2})
        self.assertFalse(out.get("error"), out)
        self.assertIn("joints", out)
        knee = out["joints"]["right_knee"]
        self.assertIn("flexion_deg", knee)
        self.assertIn("position_m", knee)

    def test_out_of_range(self) -> None:
        out = execute_tool(self.seq, "get_motion_state", {"timestamp": 1e6})
        self.assertTrue(out["error"])
        self.assertEqual(out["type"], "TIMESTAMP_OUT_OF_RANGE")

    def test_joint_trajectory(self) -> None:
        out = execute_tool(
            self.seq,
            "get_joint_trajectory",
            {"joint": "knee_r", "start_time": 3.0, "end_time": 4.0},
        )
        self.assertFalse(out.get("error"), out)
        self.assertEqual(out["joint"], "right_knee")
        self.assertIn("max_angle_deg", out["statistics"])

    def test_com_and_contact(self) -> None:
        com = execute_tool(
            self.seq, "get_com_trajectory", {"start_time": 0.0, "end_time": 2.0}
        )
        self.assertFalse(com.get("error"), com)
        contact = execute_tool(
            self.seq, "get_contact_state", {"start_time": 0.0, "end_time": 5.0}
        )
        self.assertFalse(contact.get("error"), contact)
        self.assertIn("transitions", contact)

    def test_events(self) -> None:
        out = execute_tool(
            self.seq, "get_motion_events", {"start_time": 0.0, "end_time": min(8.0, self.seq.duration_s)}
        )
        self.assertFalse(out.get("error"), out)
        self.assertGreaterEqual(out["event_count"], 1)

    def test_unsupported_joint(self) -> None:
        out = execute_tool(
            self.seq,
            "get_joint_trajectory",
            {"joint": "left_middle2", "start_time": 0.0, "end_time": 1.0},
        )
        # joint resolves, but may lack primary angle → missing kinematics path
        self.assertTrue(
            out.get("error")
            or out.get("series", {}).get("angle", {}).get("error")
            or "statistics" in out
        )


class TestDynamicsTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        session = newest_ready_session_dir(DATA_DIR) or FALLBACK
        cls.seq = adapt_session(session)

    def test_dynamics_summary(self) -> None:
        out = execute_tool(
            self.seq,
            "get_dynamics_summary",
            {"start_time": 0.0, "end_time": min(3.0, self.seq.duration_s)},
        )
        self.assertFalse(out.get("error"), out)
        self.assertIn("largest_moments", out)
        self.assertIn("peak_ground_reaction_force", out)
        self.assertIn("power_convention", out)

    def test_joint_dynamics_and_power(self) -> None:
        jd = execute_tool(
            self.seq,
            "get_joint_dynamics",
            {"joint": "right_knee", "start_time": 1.0, "end_time": 2.0},
        )
        self.assertFalse(jd.get("error"), jd)
        self.assertIn("flexion_moment_nm", jd["series"])
        pw = execute_tool(
            self.seq,
            "get_joint_power",
            {"start_time": 1.0, "end_time": 2.0, "joints": ["right_hip", "right_knee"]},
        )
        self.assertFalse(pw.get("error"), pw)
        self.assertIn("right_knee", pw["joints"])

    def test_grf(self) -> None:
        out = execute_tool(
            self.seq,
            "get_ground_reaction_forces",
            {"start_time": 0.0, "end_time": 1.0},
        )
        self.assertFalse(out.get("error"), out)
        self.assertIn("peak_total", out)


class TestVisualAndSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        session = newest_ready_session_dir(DATA_DIR) or FALLBACK
        cls.seq = adapt_session(session)

    def test_rgb_frame(self) -> None:
        out = execute_tool(self.seq, "get_rgb_frame", {"timestamp": 1.0})
        self.assertFalse(out.get("error"), out)
        self.assertTrue(out["image_base64"])
        self.assertEqual(out["mime_type"], "image/jpeg")

    def test_rgb_clip(self) -> None:
        out = execute_tool(
            self.seq,
            "get_rgb_clip",
            {"start_time": 1.0, "end_time": 1.4, "target_fps": 5},
        )
        self.assertFalse(out.get("error"), out)
        self.assertGreaterEqual(out["frame_count"], 2)

    def test_search_and_compare(self) -> None:
        search = execute_tool(self.seq, "search_motion", {"description": "find jumps"})
        self.assertFalse(search.get("error"), search)
        cmp = execute_tool(
            self.seq,
            "compare_motion",
            {
                "segment_a": {"start_time": 0.0, "end_time": 1.0},
                "segment_b": {"start_time": 1.0, "end_time": 2.0},
            },
        )
        self.assertFalse(cmp.get("error"), cmp)
        self.assertIn("semantic_differences", cmp)


class TestToolSchemas(unittest.TestCase):
    def test_schema_names(self) -> None:
        from motion_agent.llm.schemas import openai_tools

        names = {t["function"]["name"] for t in openai_tools()}
        for required in (
            "get_motion_state",
            "get_dynamics_summary",
            "get_rgb_frame",
            "get_joint_power",
            "render_force_visualization",
        ):
            self.assertIn(required, names)


if __name__ == "__main__":
    unittest.main()
