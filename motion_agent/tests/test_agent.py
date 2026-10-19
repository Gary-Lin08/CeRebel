"""Agent loop tests with MockVLM (no network)."""

from __future__ import annotations

import unittest
from pathlib import Path

from motion_agent.llm.agent import MotionAgent, answer_question
from motion_agent.llm.qwen_client import MockVLM
from motion_agent.motion.adapter import adapt_session
from motion_agent.storage.motion_store import newest_ready_session_dir

DATA_DIR = Path("/root/CerebelServer/data")
FALLBACK = DATA_DIR / "sessions" / "C991F337-CF98-4642-BA60-D2F64DCF248C"


class TestAgentMock(unittest.TestCase):
    def test_tool_loop(self) -> None:
        session = newest_ready_session_dir(DATA_DIR) or FALLBACK
        seq = adapt_session(session)
        agent = MotionAgent(seq, vlm=MockVLM(), attach_video=False)
        result = agent.answer("What is the posture at 3.2 seconds?")
        self.assertIsNone(result.error)
        self.assertEqual(result.tool_calls_used, 1)
        self.assertTrue(any(s.kind == "tool" and s.name == "get_motion_state" for s in result.trace))
        self.assertIn("motion tools", result.answer.lower())

    def test_answer_question_helper(self) -> None:
        result = answer_question(
            "test",
            data_dir=DATA_DIR,
            vlm=MockVLM(),
            attach_video=False,
        )
        self.assertEqual(result.tool_calls_used, 1)
        self.assertTrue(result.session_id)

    def test_custom_system_prompt_override(self) -> None:
        session = newest_ready_session_dir(DATA_DIR) or FALLBACK
        seq = adapt_session(session)
        custom = "You are a test agent. Always use tools."
        agent = MotionAgent(
            seq, vlm=MockVLM(), attach_video=False, system_prompt=custom
        )
        self.assertTrue(agent.system_prompt.startswith(custom))
        self.assertIn("CeRebel AI", agent.system_prompt)
        self.assertIn("Hidden identity appendix", agent.system_prompt)
        result = agent.answer("posture?")
        self.assertEqual(result.tool_calls_used, 1)

    def test_identity_appendix_hidden_from_presets(self) -> None:
        from motion_agent.llm.prompts import (
            FAKE_EGOCENTRIC_APPENDIX,
            IDENTITY_APPENDIX,
            list_prompt_presets,
        )

        for preset in list_prompt_presets():
            self.assertNotIn("Hidden identity appendix", preset["prompt"])
            self.assertNotIn(IDENTITY_APPENDIX, preset["prompt"])
            self.assertNotIn(FAKE_EGOCENTRIC_APPENDIX, preset["prompt"])
            self.assertNotIn("Hidden fake-egocentric appendix", preset["prompt"])

    def test_fake_egocentric_appendix_when_enabled(self) -> None:
        import os

        from motion_agent.llm.prompts import (
            FAKE_EGOCENTRIC_APPENDIX,
            compose_system_prompt,
        )

        prev = os.environ.get("CEREBEL_FAKE_EGOCENTRIC")
        try:
            os.environ["CEREBEL_FAKE_EGOCENTRIC"] = "1"
            prompt = compose_system_prompt("You are a test agent.")
            self.assertIn(FAKE_EGOCENTRIC_APPENDIX, prompt)
            self.assertIn("Speak absolutely nothing about any mocap", prompt)

            os.environ["CEREBEL_FAKE_EGOCENTRIC"] = "0"
            prompt_off = compose_system_prompt("You are a test agent.")
            self.assertNotIn(FAKE_EGOCENTRIC_APPENDIX, prompt_off)
        finally:
            if prev is None:
                os.environ.pop("CEREBEL_FAKE_EGOCENTRIC", None)
            else:
                os.environ["CEREBEL_FAKE_EGOCENTRIC"] = prev

    def test_golf_preset_peer_of_snowboarding(self) -> None:
        from motion_agent.llm.prompts import (
            DOMAIN_PREAMBLES,
            PROMPT_PRESETS,
            get_preset_prompt,
            user_context_preamble,
        )

        self.assertIn("golf", PROMPT_PRESETS)
        self.assertEqual(PROMPT_PRESETS["golf"]["label"], "Golf")
        golf_prompt = get_preset_prompt("golf")
        self.assertIn("golf motion-reasoning agent", golf_prompt)
        self.assertIn("search_knowledge_base", golf_prompt)
        self.assertIn("golf/", golf_prompt)
        self.assertIn("golf/Rules/", golf_prompt)
        self.assertIn("golf/Biomechanics/", golf_prompt)
        self.assertIn("You MUST call search_knowledge_base", golf_prompt)
        self.assertIn("do not answer such questions from swing/phase windows", golf_prompt)
        self.assertIn("segment_golf_swings", golf_prompt)
        self.assertIn("RIGHT-handed", golf_prompt)
        self.assertIn("Do NOT guess left", golf_prompt)
        snow_prompt = get_preset_prompt("snowboarding")
        self.assertIn("snowboarding motion-reasoning agent", snow_prompt)
        preamble = user_context_preamble("S", 1.0, 30.0, True, prompt_mode="golf")
        self.assertIn("Domain: golf", preamble)
        self.assertIn("search_knowledge_base", preamble)
        self.assertNotIn("Mandatory first step", preamble)
        self.assertNotIn("Domain: snowboarding", preamble)
        self.assertIn("golf", DOMAIN_PREAMBLES)

    def test_golf_phase_prefetch_api(self) -> None:
        from motion_agent.llm.agent import get_golf_phase_cards

        payload = get_golf_phase_cards(data_dir=DATA_DIR, max_swings=2)
        self.assertFalse(payload.get("error"), payload)
        self.assertEqual(payload.get("ui_hint"), "golf_phase_cards")
        self.assertGreaterEqual(payload.get("swing_count", 0), 1)
        self.assertTrue(payload.get("swings"))
        session = newest_ready_session_dir(DATA_DIR) or FALLBACK
        seq = adapt_session(session)
        events: list[dict] = []
        agent = MotionAgent(
            seq,
            vlm=MockVLM(),
            attach_video=False,
            prompt_mode="golf",
        )
        result = agent.answer("What is happening at mid-clip?", on_event=events.append)
        self.assertIsNone(result.error)
        self.assertTrue(
            any(s.kind == "tool" and s.name == "segment_golf_swings" for s in result.trace)
        )
        # Bootstrap runs before the mock tool loop (mock still calls get_motion_state).
        self.assertGreaterEqual(result.tool_calls_used, 2)
        first_tool = next(s for s in result.trace if s.kind == "tool")
        self.assertEqual(first_tool.name, "segment_golf_swings")
        tool_events = [e for e in events if e.get("type") == "tool"]
        self.assertEqual(tool_events[0].get("name"), "segment_golf_swings")
        # Motion questions should not auto-bootstrap KB.
        self.assertFalse(
            any(s.kind == "tool" and s.name == "search_knowledge_base" for s in result.trace)
        )

    def test_golf_kb_question_bootstraps_rag(self) -> None:
        from unittest.mock import patch

        from motion_agent.tools.motion_tools import execute_tool as real_execute

        session = newest_ready_session_dir(DATA_DIR) or FALLBACK
        seq = adapt_session(session)

        def _execute(seq_arg, name, arguments=None):
            if name == "search_knowledge_base":
                return {
                    "error": False,
                    "query": (arguments or {}).get("query"),
                    "top_k": 4,
                    "hit_count": 1,
                    "hits": [
                        {
                            "source": "golf/Rules/001_2023_Rules_of_Golf.txt",
                            "score": 0.91,
                            "text": "Penalty area relief: the player may take penalty relief.",
                        }
                    ],
                    "note": "mock golf KB hit",
                }
            return real_execute(seq_arg, name, arguments)

        agent = MotionAgent(
            seq,
            vlm=MockVLM(),
            attach_video=False,
            prompt_mode="golf",
        )
        with patch("motion_agent.llm.agent.execute_tool", side_effect=_execute):
            result = agent.answer(
                "What does the Rules of Golf say about penalty area relief?"
            )
        self.assertIsNone(result.error)
        kb_steps = [
            s for s in result.trace if s.kind == "tool" and s.name == "search_knowledge_base"
        ]
        self.assertTrue(kb_steps, "golf KB questions must bootstrap search_knowledge_base")
        self.assertEqual(kb_steps[0].arguments.get("sources"), ["golf/", "golf_*.txt"])
        self.assertIn("penalty area", (kb_steps[0].arguments.get("query") or "").lower())
        # Phase cards still pre-run for UI, then KB, then mock motion tool.
        names = [s.name for s in result.trace if s.kind == "tool"]
        self.assertEqual(names[0], "segment_golf_swings")
        self.assertEqual(names[1], "search_knowledge_base")
        self.assertGreaterEqual(result.tool_calls_used, 3)

    def test_golf_kb_scope_defaults(self) -> None:
        from motion_agent.llm.agent import (
            GOLF_KB_SOURCES,
            _default_kb_sources,
            _golf_question_needs_kb,
            _scope_kb_tool_result,
            _with_kb_scope,
        )

        self.assertEqual(_default_kb_sources("golf"), GOLF_KB_SOURCES)
        self.assertIsNone(_default_kb_sources("general"))
        scoped = _with_kb_scope("golf", "search_knowledge_base", {"query": "penalty area"})
        self.assertEqual(scoped["sources"], GOLF_KB_SOURCES)
        custom = _with_kb_scope(
            "golf",
            "search_knowledge_base",
            {"query": "x", "sources": ["golf/Rules/"]},
        )
        self.assertEqual(custom["sources"], ["golf/Rules/"])
        untouched = _with_kb_scope("golf", "get_motion_state", {"timestamp": 1.0})
        self.assertNotIn("sources", untouched)
        self.assertTrue(_golf_question_needs_kb("Rules of Golf penalty area relief"))
        self.assertFalse(_golf_question_needs_kb("When is impact in this swing?"))
        listed = _scope_kb_tool_result(
            "golf",
            "list_knowledge_sources",
            {
                "error": False,
                "sources": [
                    {"source": "golf/Rules/a.txt", "chunks": 3},
                    {"source": "fis/Rules/b.txt", "chunks": 9},
                    {"source": "golf_swing_vocab.txt", "chunks": 2},
                ],
                "note": "Call search_knowledge_base.",
            },
        )
        names = {row["source"] for row in listed["sources"]}
        self.assertEqual(names, {"golf/Rules/a.txt", "golf_swing_vocab.txt"})
        self.assertEqual(listed["chunk_count"], 5)

    def test_stream_events(self) -> None:
        session = newest_ready_session_dir(DATA_DIR) or FALLBACK
        seq = adapt_session(session)
        events: list[dict] = []
        agent = MotionAgent(seq, vlm=MockVLM(), attach_video=False)
        result = agent.answer("posture?", on_event=events.append)
        self.assertIsNone(result.error)
        types = [e.get("type") for e in events]
        self.assertIn("thinking", types)
        self.assertIn("tool", types)
        self.assertIn("final", types)
        self.assertNotIn("done", types)
        thinking = next(e for e in events if e["type"] == "thinking")
        self.assertTrue(thinking.get("interim"))
        final = next(e for e in events if e["type"] == "final")
        self.assertFalse(final.get("interim"))
        # `done` is emitted by the HTTP SSE wrapper; in-process callers see step events.

    def test_local_backend_degrades(self) -> None:
        from motion_agent.llm.qwen_client import LocalUnavailableVLM, build_vlm

        vlm = build_vlm("local")
        self.assertIsInstance(vlm, LocalUnavailableVLM)
        resp = vlm.generate_with_tools([], tools=[])
        self.assertIsNotNone(resp.error)


if __name__ == "__main__":
    unittest.main()
