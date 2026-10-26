"""The coach must not receive the user's video unless CEREBEL_LLM_ATTACH_VIDEO is on.

With attach off (production) the RGB frame/clip tools are not offered to the
model, a call to them anyway is refused without reading the video, and no frame
reaches the messages. The force render and plots are drawn from measurements and
stay available. No server data needed: tools are stubbed, the VLM is the mock.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

from motion_agent.llm import agent as agent_mod
from motion_agent.llm.agent import AgentResult, MotionAgent
from motion_agent.llm.prompts import (
    PROMPT_PRESETS,
    get_preset_prompt,
    without_video_guidance,
)
from motion_agent.llm.qwen_client import MockVLM, ToolCall, VLMResponse, build_vlm
from motion_agent.llm.schemas import RGB_TOOL_NAMES, openai_tools

# Stand-ins for base64 pixels, so a leak is easy to spot in what the model received.
FRAME_PIXELS = "RGBFRAMEPIXELS"
CLIP_PIXELS = "RGBCLIPPIXELS"
FORCE_PLOT = "FORCEPLOTPIXELS"

MODES = ("snowboarding", "general", "golf", "baseball")


def _seq() -> SimpleNamespace:
    return SimpleNamespace(
        video_id="SYNTH",
        duration_s=4.0,
        fps=30.0,
        video_path=None,
        has_dynamics=lambda: True,
    )


def _fake_execute_tool(calls: list[str]):
    def run(seq, name, arguments=None):
        calls.append(name)
        if name == "get_rgb_frame":
            return {"frame_index": 30, "mime_type": "image/jpeg", "image_base64": FRAME_PIXELS}
        if name == "get_rgb_clip":
            return {
                "frame_count": 1,
                "frames": [{"frame_index": 30, "mime_type": "image/jpeg", "image_base64": CLIP_PIXELS}],
            }
        if name == "render_force_visualization":
            return {"timestamp": 1.0, "mime_type": "image/png", "image_base64": FORCE_PLOT}
        return {"timestamp": 1.0, "joints": {}}

    return run


class _VideoCuriousVLM(MockVLM):
    """Mock VLM that asks for a frame, a clip and the force render, then answers."""

    def __init__(self) -> None:
        super().__init__()
        self.tools_seen: list[list[str]] = []
        self.sent: list[str] = []  # each request as it went to the provider

    def generate_with_tools(self, messages, tools, **kwargs):
        self.calls += 1
        self.tools_seen.append([t["function"]["name"] for t in tools])
        self.sent.append(json.dumps(messages))
        if self.calls > 1:
            return VLMResponse(content="Answer from the motion tools.")
        return VLMResponse(
            content=None,
            tool_calls=[
                ToolCall(id="c1", name="get_rgb_frame", arguments={"timestamp": 1.0}),
                ToolCall(id="c2", name="get_rgb_clip", arguments={"start_time": 0.0, "end_time": 2.0}),
                ToolCall(id="c3", name="render_force_visualization", arguments={"timestamp": 1.0}),
            ],
        )


def _run(attach_video: bool, mode: str = "snowboarding"):
    calls: list[str] = []
    vlm = _VideoCuriousVLM()
    with mock.patch.object(agent_mod, "execute_tool", _fake_execute_tool(calls)):
        agent = MotionAgent(_seq(), vlm=vlm, attach_video=attach_video, prompt_mode=mode)
        result = agent.answer("Is my lead arm straight at the top?")
    return agent, vlm, result, calls


class AttachOffTests(unittest.TestCase):
    def test_tool_list_sent_to_the_model_has_no_rgb_tools(self) -> None:
        for mode in MODES:
            with self.subTest(mode=mode):
                _agent, vlm, result, _calls = _run(attach_video=False, mode=mode)
                self.assertIsNone(result.error)
                self.assertGreaterEqual(len(vlm.tools_seen), 2)
                for names in vlm.tools_seen:
                    self.assertFalse(RGB_TOOL_NAMES & set(names), names)
                    self.assertIn("render_force_visualization", names)
                    self.assertIn("get_joint_trajectory", names)

    def test_no_rgb_frame_reaches_the_messages_even_if_called(self) -> None:
        for mode in MODES:
            with self.subTest(mode=mode):
                _agent, vlm, _result, calls = _run(attach_video=False, mode=mode)
                for sent in vlm.sent:
                    self.assertNotIn(FRAME_PIXELS, sent)
                    self.assertNotIn(CLIP_PIXELS, sent)
                    self.assertNotIn("[tool image] get_rgb", sent)
                # The video is never read for the model.
                self.assertFalse(RGB_TOOL_NAMES & set(calls), calls)
                # Measurement-drawn images still go through.
                self.assertIn(FORCE_PLOT, vlm.sent[-1])

    def test_rgb_calls_are_refused_and_carry_no_image_in_the_trace(self) -> None:
        _agent, _vlm, result, _calls = _run(attach_video=False)
        rgb_steps = [s for s in result.trace if s.kind == "tool" and s.name in RGB_TOOL_NAMES]
        self.assertEqual(len(rgb_steps), 2)
        for step in rgb_steps:
            self.assertIsNone(step.image_base64)
            self.assertEqual(step.result_summary.get("type"), "TOOL_UNAVAILABLE")
        force = [s for s in result.trace if s.name == "render_force_visualization"]
        self.assertEqual(force[0].image_base64, FORCE_PLOT)

    def test_mock_backend_from_env_is_offered_no_rgb_tools(self) -> None:
        with mock.patch.dict(os.environ, {"MOTION_AGENT_BACKEND": "mock"}):
            vlm = build_vlm()
        self.assertIsInstance(vlm, MockVLM)
        calls: list[str] = []
        with mock.patch.object(agent_mod, "execute_tool", _fake_execute_tool(calls)), mock.patch.object(
            vlm, "generate_with_tools", wraps=vlm.generate_with_tools
        ) as spy:
            result = MotionAgent(_seq(), vlm=vlm, attach_video=False).answer("posture at 3.2 s?")
        self.assertIsNone(result.error)
        self.assertTrue(spy.call_args_list)
        for call in spy.call_args_list:
            names = {t["function"]["name"] for t in call.kwargs["tools"]}
            self.assertFalse(RGB_TOOL_NAMES & names, names)

    def test_system_prompt_does_not_point_at_rgb_tools(self) -> None:
        for mode in PROMPT_PRESETS:
            with self.subTest(mode=mode):
                self.assertNotIn("RGB", without_video_guidance(get_preset_prompt(mode)))
                agent = MotionAgent(_seq(), vlm=MockVLM(), attach_video=False, prompt_mode=mode)
                for phrase in ("RGB frame", "RGB tools", "RGB video", "get_rgb"):
                    self.assertNotIn(phrase, agent.system_prompt)
        golf = without_video_guidance(get_preset_prompt("golf"))
        self.assertIn("  2) Use get_joint_trajectory on a wrist if unsure about count.", golf)
        self.assertIn("render_force_visualization", golf)


class AttachOnTests(unittest.TestCase):
    """Operator opt-in (CEREBEL_LLM_ATTACH_VIDEO=1) keeps the RGB tools, unchanged."""

    def test_rgb_tools_offered_and_frames_forwarded(self) -> None:
        _agent, vlm, _result, calls = _run(attach_video=True)
        self.assertTrue(RGB_TOOL_NAMES <= set(vlm.tools_seen[0]))
        self.assertIn(FRAME_PIXELS, vlm.sent[-1])
        self.assertIn(CLIP_PIXELS, vlm.sent[-1])
        self.assertTrue(RGB_TOOL_NAMES <= set(calls))

    def test_preset_prompt_unchanged(self) -> None:
        for mode in PROMPT_PRESETS:
            with self.subTest(mode=mode):
                agent = MotionAgent(_seq(), vlm=MockVLM(), attach_video=True, prompt_mode=mode)
                self.assertTrue(agent.system_prompt.startswith(get_preset_prompt(mode).strip()))
        self.assertEqual(openai_tools(), openai_tools(include_rgb=True))


class LibraryDefaultTests(unittest.TestCase):
    """A caller that forgets attach_video gets the private behaviour, not the video."""

    def test_motion_agent_default_offers_no_rgb_tools(self) -> None:
        calls: list[str] = []
        vlm = _VideoCuriousVLM()
        with mock.patch.object(agent_mod, "execute_tool", _fake_execute_tool(calls)):
            agent = MotionAgent(_seq(), vlm=vlm)
            agent.answer("Is my lead arm straight at the top?")
        self.assertFalse(agent.attach_video)
        for names in vlm.tools_seen:
            self.assertFalse(RGB_TOOL_NAMES & set(names), names)
        for sent in vlm.sent:
            self.assertNotIn(FRAME_PIXELS, sent)
            self.assertNotIn(CLIP_PIXELS, sent)

    def test_answer_question_default_is_off(self) -> None:
        import inspect

        self.assertIs(inspect.signature(agent_mod.answer_question).parameters["attach_video"].default, False)


class OperatorPortTests(unittest.TestCase):
    """Operator with password and X-Motion-Dev still gets video only with the env flag on."""

    operator = True

    @classmethod
    def setUpClass(cls):
        from motion_agent.app import chat_server

        cls.chat_server = chat_server
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
        cls.server.operator = cls.operator
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _attach_video_for(self, flag: str, owner: str | None = None, operator_port: int = 8093) -> bool:
        seen: dict = {}

        def fake_answer(question, **kwargs):
            seen.update(kwargs)
            return AgentResult(answer="ok", session_id="SYNTH")

        body = {"question": "hi", "video_id": "SYNTH"}
        if self.operator:
            body["backend"] = "mock"  # backend switches are refused on the public port
        body = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}/api/chat",
            data=body,
            headers={"Content-Type": "application/json", "X-Motion-Dev": "1", "X-Prompt-Password": "pw"},
        )
        cs = self.chat_server
        with mock.patch.dict(os.environ, {"CEREBEL_LLM_ATTACH_VIDEO": flag}), mock.patch.object(
            cs, "_check_prompt_password", return_value=True
        ), mock.patch.object(cs, "answer_question", fake_answer), mock.patch.object(
            cs, "build_vlm", lambda backend=None: MockVLM()
        ), mock.patch(
            "motion_agent.app.product_api.current_user", return_value=None
        ), mock.patch("motion_agent.app.product_api.media_allowed", return_value=True), mock.patch(
            "motion_agent.app.product_api.record_event"
        ), mock.patch(
            "motion_agent.app.product_session.resolve_prompt_mode_for_session",
            return_value=(None, {}),
        ), mock.patch("motion_agent.app.library.owner_of", return_value=owner), mock.patch.object(
            cs, "OPERATOR_PORT", operator_port
        ), mock.patch(
            "motion_agent.app.billing.take_question", return_value=(True, 1)
        ):
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
        return seen["attach_video"]

    def test_flag_off_means_no_video_for_the_operator_either(self) -> None:
        self.assertFalse(self._attach_video_for("0"))

    def test_flag_on_lets_the_operator_attach_video(self) -> None:
        self.assertTrue(self._attach_video_for("1"))

    def test_flag_on_never_sends_a_customers_video(self) -> None:
        # The consent promises the coach gets numbers, not video, for every account's capture.
        self.assertFalse(self._attach_video_for("1", owner="u_customer"))


class CustomerPortTests(OperatorPortTests):
    """The public port never attaches video, whatever the flag says."""

    operator = False

    def test_flag_on_lets_the_operator_attach_video(self) -> None:
        self.assertFalse(self._attach_video_for("1"))

    def test_single_shared_port_does_not_attach_either(self) -> None:
        self.assertFalse(self._attach_video_for("1", operator_port=0))


if __name__ == "__main__":
    unittest.main()
