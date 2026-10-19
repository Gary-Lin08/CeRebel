"""Rendering + plot tools against a real session."""

from __future__ import annotations

import base64
import unittest
from pathlib import Path

from motion_agent.llm.tool_registry import tool_names
from motion_agent.motion.adapter import adapt_session
from motion_agent.storage.motion_store import newest_ready_session_dir
from motion_agent.tools.motion_tools import execute_tool

DATA_DIR = Path("/root/CerebelServer/data")
FALLBACK = DATA_DIR / "sessions" / "C991F337-CF98-4642-BA60-D2F64DCF248C"


class TestRenderingTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        session = newest_ready_session_dir(DATA_DIR) or FALLBACK
        cls.seq = adapt_session(session)

    def test_force_visualization_png(self) -> None:
        out = execute_tool(self.seq, "render_force_visualization", {"timestamp": 3.2})
        self.assertFalse(out.get("error"), out)
        raw = base64.b64decode(out["image_base64"])
        self.assertGreater(len(raw), 2000)
        self.assertTrue(raw.startswith(b"\x89PNG"))
        self.assertEqual(out["mime_type"], "image/png")
        self.assertIn("grf_arrows", out["overlays"])

    def test_trajectory_plot(self) -> None:
        out = execute_tool(
            self.seq,
            "get_joint_trajectory",
            {
                "joint": "right_knee",
                "start_time": 2.0,
                "end_time": 4.0,
                "include_plot": True,
            },
        )
        self.assertFalse(out.get("error"), out)
        self.assertIn("image_base64", out)
        self.assertTrue(base64.b64decode(out["image_base64"]).startswith(b"\x89PNG"))

    def test_com_plot(self) -> None:
        out = execute_tool(
            self.seq,
            "get_com_trajectory",
            {"start_time": 0.0, "end_time": 3.0, "include_plot": True},
        )
        self.assertFalse(out.get("error"), out)
        self.assertIn("image_base64", out)

    def test_render_tool_in_schema(self) -> None:
        self.assertIn("render_force_visualization", tool_names())


class TestToolSchemas(unittest.TestCase):
    def test_required_fields(self) -> None:
        from motion_agent.llm.schemas import openai_tools

        for spec in openai_tools():
            fn = spec["function"]
            self.assertTrue(fn["name"])
            self.assertGreater(len(fn["description"]), 40)
            self.assertIn("properties", fn["parameters"])


if __name__ == "__main__":
    unittest.main()
