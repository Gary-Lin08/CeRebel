"""Labeled golf and baseball figure packs for the motion board and the model."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from motion_agent.llm.agent import (
    MotionAgent,
    _summarize_tool_result,
    _tool_message_content,
    get_sport_analysis,
)
from motion_agent.llm.prompts import PROMPT_PRESETS, get_preset_prompt
from motion_agent.llm.schemas import openai_tools
from motion_agent.motion.adapter import adapt_session
from motion_agent.storage.motion_store import newest_ready_session_dir
from motion_agent.tools.motion_tools import execute_tool

DATA_DIR = Path("/root/CerebelServer/data")


class TestSportAnalysis(unittest.TestCase):
    def setUp(self) -> None:
        session = newest_ready_session_dir(DATA_DIR)
        if session is None:
            self.skipTest("no ready session")
        self.seq = adapt_session(session)

    def test_tool_registered_and_baseball_preset(self) -> None:
        names = {t["function"]["name"] for t in openai_tools()}
        self.assertIn("build_sport_analysis", names)
        self.assertIn("baseball", PROMPT_PRESETS)
        prompt = get_preset_prompt("baseball")
        self.assertIn("build_sport_analysis", prompt)
        self.assertIn("cerebel-analysis", prompt)
        self.assertIn("RIGHT-handed", prompt)

    def test_golf_and_baseball_figures(self) -> None:
        for sport in ("golf", "baseball"):
            result = execute_tool(
                self.seq,
                "build_sport_analysis",
                {"sport": sport, "handedness": "right"},
            )
            if sport == "baseball" and result.get("type") == "SPORT_MISMATCH":
                # Golf capture must not be dressed as a pitch.
                continue
            self.assertFalse(result.get("error"), result)
            self.assertEqual(result.get("ui_hint"), "sport_motion_board")
            self.assertGreaterEqual(len(result.get("phases") or []), 1)
            series = result.get("series") or []
            self.assertGreaterEqual(len(series), 3)
            for trace in series:
                self.assertTrue(trace.get("label"))
                self.assertTrue(trace.get("unit"))
                self.assertGreaterEqual(len(trace.get("values") or []), 4)
                self.assertEqual(len(trace["values"]), len(trace["times"]))
            self.assertTrue(result.get("landmarks"))
            self.assertGreaterEqual(len(result.get("findings") or []), 2)
            for note in result["findings"]:
                self.assertTrue(note.get("why"))
                self.assertTrue(note.get("cue"))
                self.assertTrue(note.get("drill"))
                self.assertTrue(note.get("metric", {}).get("value"))
                self.assertTrue((note.get("focus") or {}).get("anchor"))
                self.assertTrue((note.get("focus") or {}).get("bones"))
            # Model throughput keeps the samples, not just a count.
            message = json.loads(_tool_message_content("build_sport_analysis", result))
            self.assertEqual(len(message["series"][0]["values"]), len(series[0]["values"]))
            summary = _summarize_tool_result("build_sport_analysis", result)
            self.assertIn("values", summary["series"][0])

    def test_prefetch_and_bootstrap(self) -> None:
        golf = get_sport_analysis("golf", data_dir=DATA_DIR)
        self.assertFalse(golf.get("error"), golf)
        self.assertEqual(golf.get("sport"), "golf")

        baseball = get_sport_analysis("baseball", data_dir=DATA_DIR)
        # Newest ready session is currently a golf swing — refuse pitch theater.
        if baseball.get("type") == "SPORT_MISMATCH" or baseball.get("error"):
            self.assertIn(
                baseball.get("type"),
                ("SPORT_MISMATCH", "ANALYSIS_FAILED", "INVALID_ARGUMENT"),
                baseball,
            )
            return

        class _Quiet:
            supports_tool_calling = lambda self: True
            supports_video = lambda self: False

            def generate_with_tools(self, messages, tools=None, enable_thinking=True, max_tokens=4096):
                from motion_agent.llm.qwen_client import VLMResponse

                tool_text = "\n".join(
                    m.get("content") or ""
                    for m in messages
                    if m.get("role") == "tool"
                )
                self.saw_samples = '"id": "separation"' in tool_text
                self.saw_values = '"values"' in tool_text
                return VLMResponse(content="The front knee is described only from the traces.")

        vlm = _Quiet()
        agent = MotionAgent(self.seq, vlm=vlm, attach_video=False, prompt_mode="baseball")
        result = agent.answer("What should this pitcher work on?")
        self.assertIsNone(result.error)
        self.assertTrue(vlm.saw_samples)
        self.assertTrue(vlm.saw_values)
        self.assertTrue(
            any(s.kind == "tool" and s.name == "build_sport_analysis" for s in result.trace)
        )


if __name__ == "__main__":
    unittest.main()
