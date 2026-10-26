"""Labeled golf and baseball figure packs for the motion board and the model."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from motion_agent.llm.agent import (
    MotionAgent,
    _summarize_tool_result,
    _tool_message_content,
    get_sport_analysis,
)
from motion_agent.llm.prompts import PROMPT_PRESETS, get_preset_prompt
from motion_agent.llm.schemas import openai_tools
from motion_agent.motion.adapter import adapt_session
from motion_agent.motion.sequence import load_motion_sequence
from motion_agent.motion.sport_analysis import build_sport_analysis
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


# --------------------------------------------------------------- late swing
# 12 s clip: the golfer walks 1 m in and turns 90° (0.5–5.5 s), fidgets, sets up,
# and only swings at 8 s: backswing to the top at 8.9 s, strike at 9.25 s.
FPS = 30
SETUP_WALK_M = 1.0
SETUP_TURN_DEG = 90.0
SETUP_TWIST_DEG = 30.0
EXTENSION_M = 0.06


def _step(t: float, a: float, b: float) -> float:
    """0 before a, 1 after b, cosine ramp between."""
    if t <= a:
        return 0.0
    if t >= b:
        return 1.0
    return 0.5 - 0.5 * math.cos(math.pi * (t - a) / (b - a))


def _pelvis_xz(t: float) -> tuple[float, float]:
    return SETUP_WALK_M * _step(t, 0.5, 5.5) + EXTENSION_M * _step(t, 9.25, 9.55), 0.0


def _pelvis_turn(t: float) -> float:
    swing = 45.0 * _step(t, 8.0, 8.9) - 75.0 * _step(t, 8.9, 9.25)
    return SETUP_TURN_DEG * _step(t, 0.5, 5.5) + swing


def _trunk_twist(t: float) -> float:
    swing = -25.0 * _step(t, 8.0, 8.9) + 25.0 * _step(t, 8.9, 9.4)
    return SETUP_TWIST_DEG * _step(t, 0.5, 5.5) + swing


def _lead_elbow(t: float) -> float:
    # Arms carried bent while walking in, long at address, 30° at the top.
    return 60.0 - 50.0 * _step(t, 4.0, 5.5) + 20.0 * _step(t, 8.0, 8.9) - 20.0 * _step(t, 8.9, 9.25)


def _late_swing_motion() -> dict:
    frames = []
    for i in range(int(12.0 * FPS)):
        t = i / FPS
        px, pz = _pelvis_xz(t)
        arc = -2.5 * _step(t, 8.0, 8.9) + 5.0 * _step(t, 8.9, 9.6)
        fidget = 0.15 * math.sin(2 * math.pi * t) * (_step(t, 1.0, 1.2) - _step(t, 2.0, 2.2))
        hand = [px + 0.3 + 0.8 * math.sin(arc), 0.8 + 0.8 * (math.cos(arc) - 1.0), pz + fidget]
        frames.append(
            {
                "time": t,
                "coordinates": {
                    "pelvis_rotation": math.radians(_pelvis_turn(t)),
                    "lumbar_rotation": math.radians(_trunk_twist(t)),
                    "elbow_flex_l": math.radians(_lead_elbow(t)),
                    "knee_angle_r": math.radians(25.0),
                },
                "bodies": {
                    "pelvis": {"p": [px, 1.0, pz], "q": [0.0, 0.0, 0.0, 1.0]},
                    "hand_l": {"p": hand},
                    "hand_r": {"p": [hand[0], hand[1], hand[2] + 0.05]},
                },
            }
        )
    return {"frames": frames}


class LateSwingBaselineTests(unittest.TestCase):
    """"From address" figures start at the address, not at the first frame of the clip."""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ik = Path(tmp) / "LATE-SWING" / "ik"
            ik.mkdir(parents=True)
            (ik / "opensim-motion.json").write_text(json.dumps(_late_swing_motion()))
            seq = load_motion_sequence(ik.parent)
        cls.board = build_sport_analysis(seq, "golf")
        cls.address_end = next(p for p in cls.board["phases"] if p["id"] == "address")["end_time"]
        cls.landmarks = {lm["id"]: lm for lm in cls.board["landmarks"]}

    def test_address_is_the_set_up_before_the_takeaway(self) -> None:
        self.assertFalse(self.board.get("error"), self.board)
        self.assertGreater(self.address_end, 7.6)
        self.assertLess(self.address_end, 8.3)

    def test_drift_turn_and_twist_are_measured_from_address(self) -> None:
        # The golfer is still from 5.5 s to the takeaway, so the address values are known.
        expected = {
            "pelvis-drift": lambda t: 100.0 * abs(_pelvis_xz(t)[0] - SETUP_WALK_M),
            "pelvis-turn": lambda t: _pelvis_turn(t) - SETUP_TURN_DEG,
            "separation": lambda t: _trunk_twist(t) - SETUP_TWIST_DEG,
        }
        for series_id, value_at in expected.items():
            for kind in ("event", "peak", "min"):
                lm = self.landmarks[f"{series_id}-{kind}"]
                self.assertAlmostEqual(lm["value"], value_at(lm["time"]), delta=0.5, msg=lm)
        # At impact the hips have not moved yet; walking in moved them 100 cm.
        self.assertLess(self.landmarks["pelvis-drift-event"]["value"], 2.0)
        self.assertAlmostEqual(self.landmarks["pelvis-drift-peak"]["value"], 100.0 * EXTENSION_M, delta=0.5)

    def test_peaks_and_minima_come_from_the_swing(self) -> None:
        for lm in self.board["landmarks"]:
            self.assertGreaterEqual(lm["time"], self.address_end - 1e-6, lm)

    def test_findings_read_address_at_the_set_up(self) -> None:
        findings = {f["id"]: f for f in self.board["findings"]}
        # Lead elbow 10° at address, 30° at the top (it was 60° while walking in).
        self.assertIn("lead-arm", findings)
        self.assertAlmostEqual(findings["lead-arm"]["goal"]["value"], 10.0, delta=1.0)
        # 25° of twist at the top counts from address, not from the walk-in.
        self.assertNotIn("separation", findings)
        self.assertTrue(any("Separation at the top" in h for h in self.board["highlights"]))
        # Early extension is the 6 cm the hips move after impact.
        self.assertIn("early-extension", findings)
        self.assertEqual(findings["early-extension"]["metric"]["value"], "6 cm")


if __name__ == "__main__":
    unittest.main()
