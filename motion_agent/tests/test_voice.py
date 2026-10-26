"""Live voice coach: WebSocket framing, reply text rules, call flow, usage limits, agent voice mode."""

from __future__ import annotations

import array
import base64
import dataclasses
import json
import math
import queue
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from motion_agent.app import voice
from motion_agent.app import ws as wslib
from motion_agent.app.speech import AsrStream, SpeechProvider, Synthesizer

INTERNAL_NAMES = ("qwen", "dashscope", "opensim", "gem-x", "smpl", "fun-asr", "tts-flash", "together", "aliyun")


def _pair() -> tuple[wslib.WebSocket, wslib.WebSocket]:
    a, b = socket.socketpair()
    client = wslib.WebSocket(a, a.makefile("rb"), client=True)
    server = wslib.WebSocket(b, b.makefile("rb"), client=False)
    return client, server


class WebSocketFramingTests(unittest.TestCase):
    def test_accept_key_matches_rfc_example(self):
        self.assertEqual(wslib.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_text_and_large_binary_round_trip(self):
        client, server = _pair()
        client.send_text("héllo")
        self.assertEqual(server.recv(), (wslib.OP_TEXT, "héllo".encode("utf-8")))
        blob = bytes(range(256)) * 300  # 76,800 bytes: the 64-bit length form
        sender = threading.Thread(target=server.send_binary, args=(blob,))
        sender.start()  # bigger than the socket buffer, so read while it sends
        self.assertEqual(client.recv(), (wslib.OP_BINARY, blob))
        sender.join(2)
        client.send_binary(b"x" * 300)  # 16-bit length form, masked
        self.assertEqual(server.recv(), (wslib.OP_BINARY, b"x" * 300))
        client.close()
        server.close()

    def test_fragments_are_joined_and_pings_answered(self):
        client, server = _pair()
        client._send_frame(wslib.OP_PING, b"hi")
        mask = b"\x01\x02\x03\x04"
        for first, fin, part in ((True, False, b"ab"), (False, True, b"cd")):
            op = wslib.OP_TEXT if first else wslib.OP_CONT
            frame = bytes([(0x80 if fin else 0) | op, 0x80 | len(part)]) + mask + wslib._xor(part, mask)
            client.sock.sendall(frame)
        self.assertEqual(server.recv(), (wslib.OP_TEXT, b"abcd"))
        server.send_text("after")
        self.assertEqual(client.recv(), (wslib.OP_TEXT, b"after"))  # the pong was skipped

    def test_server_refuses_unmasked_client_frames(self):
        a, b = socket.socketpair()
        server = wslib.WebSocket(b, b.makefile("rb"), client=False)
        a.sendall(bytes([0x81, 0x02]) + b"no")
        self.assertIsNone(server.recv())
        self.assertTrue(server.closed)

    def test_close_ends_recv(self):
        client, server = _pair()
        client.close()
        self.assertIsNone(server.recv())


class ReplyTextTests(unittest.TestCase):
    def test_numbers_move_from_speech_to_screen(self):
        spoken, screen = voice.plan_reply(
            "SAY: Your hips slide toward the ball after impact, about 8 cm. That makes you stand up. "
            "Feel your trail hip stay back.\nSCREEN:\n- Pelvis drift after impact: 8 cm\n- Lead arm at the top: 152°"
        )
        self.assertEqual(spoken, ["That makes you stand up.", "Feel your trail hip stay back."])
        self.assertIn("Pelvis drift after impact: 8 cm", screen)
        self.assertIn("Lead arm at the top: 152 degrees", screen)
        self.assertIn("Your hips slide toward the ball after impact, about 8 cm.", screen)
        self.assertFalse(any(ch.isdigit() for ch in " ".join(spoken)))

    def test_spelled_out_measurements_are_not_spoken(self):
        spoken, screen = voice.plan_reply("SAY: Your hips drift about eight centimeters. Try ten slow swings.")
        self.assertEqual(spoken, ["Try ten slow swings."])
        self.assertEqual(screen, ["Your hips drift about eight centimeters."])

    def test_unformatted_answer_markdown_and_fence(self):
        spoken, screen = voice.plan_reply(
            "**Keep your spine tilt.** It stops the early extension.\n```cerebel-analysis\n{\"findings\": [1]}\n```"
        )
        self.assertEqual(spoken, ["Keep your spine tilt.", "It stops the early extension."])
        self.assertEqual(screen, [])

    def test_at_most_three_sentences_spoken(self):
        spoken, _ = voice.plan_reply("SAY: One. Two. Three. Four. Five.")
        self.assertEqual(spoken, ["One.", "Two.", "Three."])

    def test_brief_reply(self):
        brief = {
            "answer": "Early extension\n\nYour hips move toward the ball through impact.\n\n"
            "Measured: 8 cm — pelvis drift after impact\n\nWhat to feel: trail hip stays back.\n\nTry this: chair drill."
        }
        spoken, screen = voice.brief_reply(brief)
        self.assertEqual(spoken, ["Early extension.", "Your hips move toward the ball through impact.", "What to feel: trail hip stays back."])
        self.assertEqual(screen, ["8 cm — pelvis drift after impact"])

    def test_specific_spoken_questions_skip_precomputed_answers(self):
        briefs = [
            {"id": "biggest_fix", "question": "What's the biggest thing I should fix?", "answer": "a"},
            {"id": "finding_drift_pelvis", "question": "Is my pelvis drifting?", "answer": "b"},
        ]
        self.assertEqual(voice.match_voice_brief("what's the biggest thing I should fix", briefs)["id"], "biggest_fix")
        self.assertEqual(voice.match_voice_brief("What's wrong with my hips?", briefs)["id"], "finding_drift_pelvis")
        self.assertIsNone(voice.match_voice_brief("How fast were my hips turning at impact?", briefs))
        self.assertIsNone(voice.match_voice_brief("What drill should I do for that?", briefs))

    def test_screen_lines_must_be_measured_values(self):
        evidence = json.dumps({"pelvis_drift_m": 0.0812, "peak_deg_s": 450.2})
        self.assertIsNone(voice.screen_line_problem("Pelvis drift after impact: 8 cm", evidence))
        self.assertEqual(voice.screen_line_problem("Pelvis speed: ~450 deg/s (estimated from trajectory)", evidence), "estimate")
        self.assertEqual(voice.screen_line_problem("Pelvis turn velocity: high, inferred from steepness", evidence), "no number")
        self.assertEqual(voice.screen_line_problem("Lead arm: 152 degrees", evidence), "not in tool output")

    def test_topic_metrics_come_from_the_report(self):
        findings = [
            {"id": "early-extension", "title": "Early extension", "answer": "You're standing up\n\nMeasured: 20 cm — hips drifting toward the ball after impact"},
            {"id": "lead-arm", "title": "Lead arm bends", "answer": "Your front arm collapses\n\nMeasured: 51° — front-arm bend at the top"},
        ]
        self.assertEqual(voice.topic_metrics("How far did my hips slide forward?", findings), ["20 cm — hips drifting toward the ball after impact"])
        self.assertEqual(voice.topic_metrics("Is my lead arm okay?", findings), ["51 degrees — front-arm bend at the top"])
        self.assertEqual(voice.topic_metrics("What club should I use?", findings), [])

    def test_ungrounded_numbers(self):
        evidence = json.dumps({"pelvis_drift_m": 0.0812, "peak_deg_s": 987.31, "t": 1.42})
        self.assertEqual(voice.ungrounded_numbers("Pelvis drift: 8 cm", evidence), [])
        self.assertEqual(voice.ungrounded_numbers("Peak hip speed: 987 deg/s at 1.42 s", evidence), [])
        self.assertEqual(voice.ungrounded_numbers("Hip speed: 394 deg/s", evidence), ["394"])
        self.assertEqual(voice.ungrounded_numbers("About 2.7 rotations per second", evidence), ["2.7"])
        self.assertEqual(voice.ungrounded_numbers("Pelvis: 394 degrees per second", json.dumps({"w": 6.8813})), [])  # rad/s
        self.assertEqual(voice.ungrounded_numbers("Do 3 sets", evidence), [])
        self.assertEqual(voice.ungrounded_numbers("Peak hip speed: about 990 deg/s", evidence), [])  # nearest ten
        self.assertEqual(voice.ungrounded_numbers("Peak hip speed: about 1000 deg/s", evidence), ["1000"])


class FakeSock:
    def __init__(self) -> None:
        self.inbox: queue.Queue = queue.Queue()
        self.sent: list = []
        self.closed = False
        self.lock = threading.Lock()

    def send_text(self, text: str) -> None:
        if self.closed:
            raise wslib.WebSocketClosed()
        with self.lock:
            self.sent.append(("text", json.loads(text)))

    def send_binary(self, data: bytes) -> None:
        if self.closed:
            raise wslib.WebSocketClosed()
        with self.lock:
            self.sent.append(("bin", struct.unpack("<I", data[:4])[0], data[4:]))

    def recv(self):
        while not self.closed:
            try:
                return self.inbox.get(timeout=0.05)
            except queue.Empty:
                continue
        return None

    def close(self, *a) -> None:
        self.closed = True

    def texts(self, kind=None):
        with self.lock:
            rows = [m[1] for m in self.sent if m[0] == "text"]
        return [r for r in rows if kind is None or r.get("type") == kind]

    def audio_turns(self):
        with self.lock:
            return [m[1] for m in self.sent if m[0] == "bin"]


class FakeAsr(AsrStream):
    def __init__(self, on_partial, on_final, on_error) -> None:
        self.on_partial, self.on_final, self.on_error = on_partial, on_final, on_error
        self.audio = 0

    def send_audio(self, pcm: bytes) -> None:
        self.audio += len(pcm)

    def close(self) -> None:
        pass


class FakeTts(Synthesizer):
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.spoken: list[str] = []

    def synthesize(self, text, on_audio, cancel) -> bool:
        self.spoken.append(text)
        for _ in range(3):
            if cancel.is_set():
                return False
            on_audio(b"\x01\x00" * 240)
            time.sleep(self.delay)
        return True


class FakeProvider(SpeechProvider):
    def __init__(self, delay: float = 0.0, fail: bool = False) -> None:
        self.name = "fake-%d" % id(self)
        self.delay, self.fail = delay, fail
        self.asr: FakeAsr | None = None
        self.tts: FakeTts | None = None

    def open_asr(self, on_partial, on_final, on_error):
        if self.fail:
            raise OSError("down")
        self.asr = FakeAsr(on_partial, on_final, on_error)
        return self.asr

    def open_tts(self):
        tts = FakeTts(self.delay)
        if self.tts is None:
            self.tts = tts
        return tts


def _wait(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class VoiceCallTests(unittest.TestCase):
    def _start(self, provider=None, **kw):
        sock = FakeSock()
        provider = provider or FakeProvider()
        kw.setdefault("answer_fn", lambda q, h: SimpleNamespace(answer="SAY: Keep your trail hip back.", evidence=[], error=None))
        call = voice.VoiceCall(sock, provider, seconds=kw.pop("seconds", 60), **kw)
        thread = threading.Thread(target=call.run, daemon=True)
        thread.start()
        self.assertTrue(_wait(lambda: sock.texts("ready")))
        self.addCleanup(lambda: (call.end("hangup"), thread.join(2)))
        return sock, provider, call, thread

    def test_agent_turn_speaks_without_numbers_and_shows_them(self):
        answers = []

        def answer(question, history):
            answers.append((question, list(history)))
            return SimpleNamespace(
                answer="SAY: Your hips slide toward the ball. Feel the trail hip stay back.\nSCREEN:\n- Pelvis drift after impact: 8 cm\n- Made up: 42 cm",
                evidence=['{"pelvis_drift_m": 0.0812}'],
                error=None,
            )

        sock, provider, call, _ = self._start(answer_fn=answer)
        _wait(lambda: voice._ACK_CACHE.get((provider.name + ":", voice.ACK_PHRASES[0])))
        provider.asr.on_final("How are my hips?")
        self.assertTrue(_wait(lambda: sock.texts("audio_end")))
        coach = sock.texts("coach")[0]
        self.assertEqual(coach["say"], "Your hips slide toward the ball. Feel the trail hip stay back.")
        self.assertEqual(coach["screen"], ["Pelvis drift after impact: 8 cm"])  # 42 cm is in no tool output
        self.assertEqual(call.stats["ungrounded"], 1)
        self.assertEqual(sock.texts("user")[0]["text"], "How are my hips?")
        self.assertEqual(set(sock.audio_turns()), {1})
        self.assertIn("Feel the trail hip stay back.", call.tts.spoken)
        provider.asr.on_final("What drill fixes that?")
        self.assertTrue(_wait(lambda: len(sock.texts("audio_end")) == 2))
        self.assertEqual(answers[1][1][0], {"role": "user", "content": "How are my hips?"})
        self.assertIn("Pelvis drift after impact: 8 cm", answers[1][1][1]["content"])

    def test_instant_brief_skips_the_model(self):
        brief = {"id": "biggest_fix", "question": "What's the biggest thing I should fix?", "answer": "Early extension\n\nMeasured: 8 cm — drift\n\nWhat to feel: stay in posture."}
        called = []
        sock, provider, call, _ = self._start(briefs_fn=lambda: [brief], answer_fn=lambda q, h: called.append(q))
        self.assertTrue(_wait(lambda: call.briefs))
        provider.asr.on_final("What's the biggest thing I should fix?")
        self.assertTrue(_wait(lambda: sock.texts("audio_end")))
        coach = sock.texts("coach")[0]
        self.assertTrue(coach["instant"])
        self.assertEqual(coach["screen"], ["8 cm — drift"])
        self.assertEqual(called, [])

    def test_new_question_interrupts_the_current_answer(self):
        sock, provider, call, _ = self._start(provider=FakeProvider(delay=0.05))
        provider.asr.on_final("First question?")
        self.assertTrue(_wait(lambda: 1 in sock.audio_turns()))
        provider.asr.on_final("Actually, second question?")
        self.assertTrue(_wait(lambda: any(m["turn"] == 2 for m in sock.texts("audio_end"))))
        turns = sock.audio_turns()
        self.assertEqual(turns[-1], 2)
        self.assertNotIn(1, turns[turns.index(2):])  # nothing from turn 1 after turn 2 starts
        self.assertEqual(call.stats["interrupted"], 1)
        self.assertNotIn(1, [m["turn"] for m in sock.texts("audio_end")])

    def test_interrupt_message_stops_speech(self):
        sock, provider, call, _ = self._start(provider=FakeProvider(delay=0.1))
        provider.asr.on_final("Tell me about my swing?")
        self.assertTrue(_wait(lambda: sock.texts("coach")))
        sock.inbox.put((wslib.OP_TEXT, json.dumps({"type": "interrupt"}).encode()))
        self.assertTrue(_wait(lambda: call.cancel.is_set()))
        self.assertTrue(_wait(lambda: not call._busy()))
        self.assertEqual(sock.texts("audio_end"), [])

    def test_fillers_are_ignored_and_audio_reaches_recognition(self):
        sock, provider, call, _ = self._start()
        sock.inbox.put((wslib.OP_BINARY, b"\x00\x00" * 800))
        self.assertTrue(_wait(lambda: provider.asr.audio == 1600))
        provider.asr.on_final("um")
        time.sleep(0.1)
        self.assertEqual(sock.texts("user"), [])

    def test_daily_question_limit_ends_the_call(self):
        sock, provider, call, thread = self._start(take_question=lambda: (False, "You've reached today's question limit."))
        provider.asr.on_final("One more question?")
        self.assertTrue(_wait(lambda: sock.texts("end")))
        self.assertEqual(sock.texts("end")[0]["reason"], "limit")

    def test_time_limit_ends_the_call(self):
        sock, provider, call, thread = self._start(seconds=1)
        self.assertTrue(_wait(lambda: sock.texts("end"), timeout=4))
        self.assertEqual(sock.texts("end")[0]["reason"], "time")
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_services_down_gives_a_plain_message(self):
        sock = FakeSock()
        call = voice.VoiceCall(sock, FakeProvider(fail=True), answer_fn=lambda q, h: None, seconds=60)
        with self.assertLogs(voice.LOG, "ERROR"):
            call.run()
        end = sock.texts("end")[0]
        self.assertEqual(end["reason"], "error")
        self.assertIn("keep typing", end["message"])

    def test_model_error_and_exceptions_stay_generic(self):
        def boom(q, h):
            raise RuntimeError("DashScope qwen3.7-plus OpenSim exploded")

        sock, provider, call, _ = self._start(answer_fn=boom)
        with self.assertLogs(voice.LOG, "ERROR"):
            provider.asr.on_final("Why is this failing?")
            self.assertTrue(_wait(lambda: sock.texts("coach")))
        text = json.dumps(sock.texts()).lower()
        for name in INTERNAL_NAMES:
            self.assertNotIn(name, text)


class DeviceSpeechCallTests(unittest.TestCase):
    """The browser hears and speaks; the call carries text only."""

    def _start(self, answer_fn, briefs_fn=None):
        sock = FakeSock()
        call = voice.VoiceCall(sock, None, answer_fn=answer_fn, seconds=60, briefs_fn=briefs_fn)
        thread = threading.Thread(target=call.run, daemon=True)
        thread.start()
        self.assertTrue(_wait(lambda: sock.texts("ready")))
        self.addCleanup(lambda: (call.end("hangup"), thread.join(2)))
        return sock, call

    def say(self, sock, text):
        sock.inbox.put((wslib.OP_TEXT, json.dumps({"type": "say", "text": text}).encode()))

    def test_text_turn_with_opener_and_no_audio(self):
        answer = lambda q, h: SimpleNamespace(
            answer="SAY: Your hips slide toward the ball. Keep the trail hip back.\nSCREEN:\n- Pelvis drift after impact: 8 cm",
            evidence=['{"pelvis_drift_m": 0.08}'], error=None)
        sock, call = self._start(answer)
        self.assertEqual(sock.texts("ready")[0]["speech"], "device")
        self.say(sock, "How are my hips?")
        self.assertTrue(_wait(lambda: sock.texts("audio_end")))
        thinking = [m for m in sock.texts("state") if m["state"] == "thinking"][0]
        self.assertEqual(thinking["ack"], voice.ACK_PHRASES[0])
        coach = sock.texts("coach")[0]
        self.assertEqual(coach["say"], "Your hips slide toward the ball. Keep the trail hip back.")
        self.assertEqual(coach["screen"], ["Pelvis drift after impact: 8 cm"])
        self.assertEqual(sock.audio_turns(), [])
        self.assertEqual(len(call.stats["reply_ms"]), 1)
        self.assertEqual(call.summary()["speech"], "device")

    def test_first_question_waits_for_precomputed_answers(self):
        brief = {"id": "biggest_fix", "question": "What's the biggest thing I should fix?", "answer": "Early extension\n\nMeasured: 8 cm — drift"}

        def slow_briefs():
            time.sleep(0.4)
            return {"briefs": [brief], "findings": []}

        called = []
        sock, call = self._start(lambda q, h: called.append(q), briefs_fn=slow_briefs)
        self.say(sock, "What's the biggest thing I should fix?")
        self.assertTrue(_wait(lambda: sock.texts("coach")))
        self.assertTrue(sock.texts("coach")[0]["instant"])
        self.assertEqual(called, [])

    def test_new_sentence_cancels_the_pending_answer(self):
        gate = threading.Event()

        def slow(q, h):
            if q.startswith("First"):
                gate.wait(2)
            return SimpleNamespace(answer="SAY: " + q, evidence=[], error=None)

        sock, call = self._start(slow)
        self.say(sock, "First question about my swing?")
        self.assertTrue(_wait(lambda: sock.texts("user")))
        self.say(sock, "Second question about my arms?")
        self.assertTrue(_wait(lambda: any(m["turn"] == 2 for m in sock.texts("audio_end"))))
        gate.set()
        time.sleep(0.2)
        self.assertEqual([m["turn"] for m in sock.texts("coach")], [2])
        self.assertEqual(call.stats["interrupted"], 1)

    def test_audio_frames_are_ignored(self):
        sock, call = self._start(lambda q, h: None)
        sock.inbox.put((wslib.OP_BINARY, b"\x00\x00" * 800))
        time.sleep(0.1)
        self.assertFalse(sock.closed)
        self.assertIsNone(call.asr)


class VoiceContextTests(unittest.TestCase):
    def test_board_is_compacted(self):
        from motion_agent.llm.agent import _compact_board, _compact_boards_for_voice

        board = {
            "tool": "build_sport_analysis", "stage_label": "long label", "disclosure": {"x": 1},
            "phases": [{"id": "address", "start_time": 0.0, "end_time": 0.3}],
            "series": [{"id": "pelvis-drift", "label": "Pelvis drift", "unit": "cm", "times": [0.1] * 300, "values": [1.5] * 300}],
            "landmarks": [{"id": "pelvis-drift-event", "value": 8.1, "unit": "cm"}],
            "findings": [{"id": "early-extension", "metric": {"value": "8 cm"}}],
            "note_for_model": "cite samples",
        }
        out = json.loads(_compact_board(json.dumps(board)))
        self.assertEqual(out["series"], [{"id": "pelvis-drift", "label": "Pelvis drift", "unit": "cm"}])
        self.assertEqual(out["landmarks"], board["landmarks"])
        self.assertEqual(out["findings"], board["findings"])
        self.assertNotIn("stage_label", out)
        self.assertIn("get_joint_trajectory", out["note_for_model"])
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "b1", "function": {"name": "build_sport_analysis"}}]},
            {"role": "tool", "tool_call_id": "b1", "content": json.dumps(board)},
            {"role": "tool", "tool_call_id": "other", "content": json.dumps(board)},
        ]
        _compact_boards_for_voice(messages)
        self.assertLess(len(messages[1]["content"]), len(messages[2]["content"]) / 3)
        self.assertEqual(json.loads(messages[2]["content"]), board)  # other tools untouched
        self.assertEqual(_compact_board("not json"), "not json")


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.user = {"user_id": "u1"}

    def test_trial_then_upgrade(self):
        with mock.patch.dict("os.environ", {"CEREBEL_VOICE_PLAN": "trial", "CEREBEL_VOICE_TRIAL_S": "180"}):
            self.assertEqual(voice.voice_allowance(self.dir, None, False)["need"], "signin")
            first = voice.voice_allowance(self.dir, self.user, False)
            self.assertEqual((first["seconds"], first["kind"], first["need"]), (180, "trial", None))
            voice.record_voice_seconds(self.dir, "u1", "trial", 100)
            self.assertEqual(voice.voice_allowance(self.dir, self.user, False)["seconds"], 80)
            voice.record_voice_seconds(self.dir, "u1", "trial", 75)
            self.assertEqual(voice.voice_allowance(self.dir, self.user, False)["need"], "upgrade")
            pro = voice.voice_allowance(self.dir, self.user, True)
            self.assertEqual((pro["kind"], pro["need"]), ("pro", None))

    def test_pro_monthly_allowance_and_call_cap(self):
        env = {"CEREBEL_VOICE_PLAN": "pro", "CEREBEL_VOICE_PRO_MONTHLY_S": "900", "CEREBEL_VOICE_MAX_CALL_S": "600"}
        with mock.patch.dict("os.environ", env):
            self.assertEqual(voice.voice_allowance(self.dir, self.user, False)["need"], "upgrade")
            self.assertEqual(voice.voice_allowance(self.dir, self.user, True)["seconds"], 600)
            voice.record_voice_seconds(self.dir, "u1", "pro", 500)
            self.assertEqual(voice.voice_allowance(self.dir, self.user, True)["seconds"], 400)
            data = json.loads((self.dir / "voice_usage.json").read_text())
            data["users"]["u1"]["month"] = "2000-01"
            (self.dir / "voice_usage.json").write_text(json.dumps(data))
            self.assertEqual(voice.voice_allowance(self.dir, self.user, True)["seconds"], 600)  # new month

    def test_open_plan_for_local_demos(self):
        with mock.patch.dict("os.environ", {"CEREBEL_VOICE_PLAN": "all"}):
            self.assertIsNone(voice.voice_allowance(self.dir, None, False)["need"])


class RecordingVLM:
    def __init__(self, tool_rounds: int) -> None:
        self.calls: list[dict] = []
        self.tool_rounds = tool_rounds

    def supports_tool_calling(self):
        return True

    def supports_video(self):
        return False

    def generate_with_tools(self, messages, tools, **kwargs):
        from motion_agent.llm.qwen_client import ToolCall, VLMResponse

        self.calls.append({"messages": list(messages), "tools": [t["function"]["name"] for t in tools], **kwargs})
        if tools and len(self.calls) <= self.tool_rounds:
            return VLMResponse(content=None, tool_calls=[ToolCall(id=f"c{len(self.calls)}", name="get_motion_state", arguments={"timestamp": 1.0})])
        return VLMResponse(content="SAY: Stay in posture.\nSCREEN:\n- Pelvis drift: 8 cm")


class AgentVoiceModeTests(unittest.TestCase):
    def _agent(self, vlm):
        from motion_agent.llm.agent import MotionAgent

        seq = SimpleNamespace(video_id="vid", duration_s=3.0, fps=30.0, has_dynamics=lambda: False, video_path=None)
        return MotionAgent(seq, vlm=vlm, prompt_mode="snowboarding")

    def test_voice_mode_settings(self):
        from motion_agent.llm.agent import VOICE_TOOLS

        vlm = RecordingVLM(tool_rounds=5)
        with mock.patch("motion_agent.llm.agent.execute_tool", return_value={"pelvis_drift_m": 0.0812}):
            result = self._agent(vlm).answer(
                "And the drill?",
                history=[{"role": "user", "content": "How are my hips?"}, {"role": "assistant", "content": "They drift."}, {"role": "system", "content": "ignored"}],
                voice=True,
            )
        first = vlm.calls[0]
        self.assertFalse(first["enable_thinking"])
        self.assertTrue(set(first["tools"]) <= VOICE_TOOLS)
        self.assertIn("get_joint_trajectory", first["tools"])
        self.assertIn("Live voice call", first["messages"][0]["content"])
        self.assertEqual([m["role"] for m in first["messages"][:3]], ["system", "user", "assistant"])
        self.assertEqual(first["messages"][1]["content"], "How are my hips?")
        # Two tool rounds, then the model must answer without tools.
        self.assertEqual(len(vlm.calls), 3)
        self.assertEqual(vlm.calls[2]["tools"], [])
        self.assertIn("0.0812", result.evidence[0])
        self.assertTrue(result.answer.startswith("SAY:"))

    def test_speed_questions_must_measure_first(self):
        vlm = RecordingVLM(tool_rounds=1)
        with mock.patch("motion_agent.llm.agent.execute_tool", return_value={"peak_deg_s": 394.27}):
            self._agent(vlm).answer("How fast were my hips turning at impact?", voice=True)
        self.assertEqual(vlm.calls[0]["tool_choice"], {"type": "function", "function": {"name": "get_joint_trajectory"}})
        self.assertEqual(vlm.calls[1]["tool_choice"], "auto")
        other = RecordingVLM(tool_rounds=0)
        self._agent(other).answer("What should I fix first?", voice=True)
        self.assertEqual(other.calls[0]["tool_choice"], "auto")
        text = RecordingVLM(tool_rounds=0)
        self._agent(text).answer("How fast were my hips turning?")
        self.assertEqual(text.calls[0]["tool_choice"], "auto")

    def test_text_mode_unchanged(self):
        vlm = RecordingVLM(tool_rounds=0)
        result = self._agent(vlm).answer("How are my hips?")
        self.assertTrue(vlm.calls[0]["enable_thinking"])
        self.assertNotIn("Live voice call", vlm.calls[0]["messages"][0]["content"])
        self.assertIn("get_rgb_frame", vlm.calls[0]["tools"])
        self.assertEqual(result.evidence, [])


class ThinkingFlagTests(unittest.TestCase):
    def test_false_is_sent_explicitly(self):
        from motion_agent.llm.qwen_client import QwenDashScopeVLM

        sent = []
        message = SimpleNamespace(content="ok", tool_calls=None, model_extra={})
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: sent.append(kw) or SimpleNamespace(choices=[SimpleNamespace(message=message)])
        )))
        vlm = QwenDashScopeVLM(api_key="test-key")
        vlm._client = fake
        with mock.patch.dict("os.environ", {"MOTION_AGENT_ENABLE_THINKING": "0"}):
            vlm.generate([{"role": "user", "content": "hi"}])
        vlm.generate([{"role": "user", "content": "hi"}], enable_thinking=False)
        with mock.patch.dict("os.environ", {"MOTION_AGENT_ENABLE_THINKING": "1"}):
            vlm.generate([{"role": "user", "content": "hi"}])
        self.assertEqual([kw["extra_body"] for kw in sent], [{"enable_thinking": False}, {"enable_thinking": False}, {"enable_thinking": True}])


class FakeTogether:
    """A local stand-in that speaks Together AI's documented realtime STT and TTS WebSocket messages."""

    def __init__(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        fake = self
        self.requests: list[tuple[str, dict]] = []
        self.messages: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                fake.requests.append((self.path, dict(self.headers)))
                sock = wslib.server_upgrade(self)
                try:
                    if self.path.startswith("/v1/realtime"):
                        fake._stt(sock)
                    else:
                        fake._tts(sock)
                except (OSError, wslib.WebSocketClosed):
                    pass  # the client hung up, as the cancel test does

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "ws://127.0.0.1:%d" % self.server.server_address[1]

    def _stt(self, sock) -> None:
        sock.send_text(json.dumps({"type": "session.created", "session": {"id": "s1", "model": "openai/whisper-large-v3"}}))
        appended = 0
        while True:
            msg = sock.recv()
            if msg is None:
                return
            ev = json.loads(msg[1])
            self.messages.append(ev)
            if ev["type"] == "input_audio_buffer.append":
                appended += 1
                if appended == 2:
                    sock.send_text(json.dumps({"type": "conversation.item.input_audio_transcription.delta", "delta": "How fast"}))
                if appended == 4:
                    sock.send_text(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "How fast were my hips turning?"}))

    def _tts(self, sock) -> None:
        def delta(ctx, pcm):
            return json.dumps({"type": "conversation.item.audio_output.delta", "item_id": "tts_1", "context_id": ctx, "delta": base64.b64encode(pcm).decode()})

        while True:
            msg = sock.recv()
            if msg is None:
                return
            ev = json.loads(msg[1])
            self.messages.append(ev)
            if ev["type"] == "input_text_buffer.commit":
                ctx = ev.get("context_id")
                sock.send_text(delta("an-older-sentence", b"\x09\x00" * 4))
                sock.send_text(delta(ctx, b"RIFF" + b"\x00" * 32 + b"data" + b"\x00" * 4 + b"\x01\x00" * 4))
                sock.send_text(delta(ctx, b"\x02\x00" * 4))
                sock.send_text(json.dumps({"type": "conversation.item.audio_output.done", "item_id": "tts_1", "context_id": ctx}))

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TogetherAdapterTests(unittest.TestCase):
    def setUp(self):
        from motion_agent.app import speech

        self.speech = speech
        self.fake = FakeTogether()
        self.addCleanup(self.fake.close)
        env = mock.patch.dict("os.environ", {"CEREBEL_VOICE_API_KEY": "test-key", "CEREBEL_VOICE_WS_BASE": self.fake.base, "CEREBEL_VOICE_END_SILENCE_MS": "600"})
        env.start()
        self.addCleanup(env.stop)

    def test_transcription_protocol(self):
        partials, finals = [], []
        asr = self.speech.TogetherProvider().open_asr(partials.append, finals.append, lambda m: None)
        self.addCleanup(asr.close)
        for i in range(4):
            asr.send_audio(bytes([i, 0]) * 800)
        self.assertTrue(_wait(lambda: finals))
        path, headers = self.fake.requests[0]
        self.assertEqual(path, "/v1/realtime?intent=transcription&model=openai%2Fwhisper-large-v3&input_audio_format=pcm_s16le_16000")
        self.assertEqual(headers.get("Authorization"), "Bearer test-key")
        self.assertEqual(headers.get("OpenAI-Beta"), "realtime=v1")
        update = self.fake.messages[0]
        self.assertEqual(update["type"], "transcription_session.updated")
        vad = update["session"]["turn_detection"]
        self.assertEqual((vad["type"], vad["min_silence_duration_ms"], vad["max_speech_duration_s"]), ("server_vad", 600, 30))
        appended = [m for m in self.fake.messages if m["type"] == "input_audio_buffer.append"]
        self.assertEqual(base64.b64decode(appended[1]["audio"]), bytes([1, 0]) * 800)
        self.assertEqual(partials, ["How fast"])
        self.assertEqual(finals, ["How fast were my hips turning?"])

    def test_synthesis_protocol(self):
        tts = self.speech.TogetherProvider().open_tts()
        self.addCleanup(tts.close)
        chunks = []
        self.assertTrue(tts.synthesize("Keep your trail hip back.", chunks.append, threading.Event()))
        self.assertEqual(chunks, [b"\x01\x00" * 4, b"\x02\x00" * 4])  # WAV header gone, older sentence skipped
        path, headers = self.fake.requests[0]
        self.assertEqual(path, "/v1/audio/speech/websocket?model=hexgrad%2FKokoro-82M&voice=af_heart&response_format=pcm&sample_rate=24000&segment=never")
        self.assertEqual(headers.get("Authorization"), "Bearer test-key")
        append, commit = self.fake.messages[:2]
        self.assertEqual((append["type"], append["text"]), ("input_text_buffer.append", "Keep your trail hip back."))
        self.assertEqual(commit, {"type": "input_text_buffer.commit", "context_id": append["context_id"]})
        # The same connection serves the next sentence.
        self.assertTrue(tts.synthesize("Stay in posture.", chunks.append, threading.Event()))
        self.assertEqual(len(self.fake.requests), 1)

    def test_cancel_drops_the_connection(self):
        tts = self.speech.TogetherProvider().open_tts()
        stop = threading.Event()
        stop.set()
        self.assertFalse(tts.synthesize("Never heard.", lambda pcm: None, stop))
        self.assertIsNone(tts.sock)

    def test_key_and_readiness(self):
        clean = {k: "" for k in ("CEREBEL_VOICE_API_KEY", "TOGETHER_API_KEY", "MOTION_AGENT_API_KEY", "MOTION_AGENT_BASE_URL")}
        with mock.patch.dict("os.environ", {**clean, "CEREBEL_VOICE_PROVIDER": "together"}):
            self.assertFalse(self.speech.provider_ready())
            with self.assertRaises(RuntimeError):
                self.speech.build_provider()
        with mock.patch.dict("os.environ", {**clean, "MOTION_AGENT_API_KEY": "coach", "MOTION_AGENT_BASE_URL": "https://api.fireworks.ai/inference/v1"}):
            self.assertIsNone(self.speech.together_key())  # another provider's key is not used
        with mock.patch.dict("os.environ", {**clean, "MOTION_AGENT_API_KEY": "coach", "MOTION_AGENT_BASE_URL": "https://api.together.xyz/v1"}):
            self.assertEqual(self.speech.together_key(), "coach")
        with mock.patch.dict("os.environ", {**clean, "TOGETHER_API_KEY": "t", "CEREBEL_VOICE_API_KEY": "v"}):
            self.assertEqual(self.speech.together_key(), "v")
            self.assertTrue(self.speech.provider_ready())
        with mock.patch.dict("os.environ", {**clean, "CEREBEL_VOICE_PROVIDER": "mock"}):
            self.assertTrue(self.speech.provider_ready())


class ServerIntegrationTests(unittest.TestCase):
    """A real server socket, the mock speech provider and a stubbed coach."""

    def setUp(self):
        from http.server import ThreadingHTTPServer

        from motion_agent.app import chat_server

        self.cs = chat_server
        tmp = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(chat_server, "SETTINGS", dataclasses.replace(chat_server.SETTINGS, data_dir=tmp)),
            mock.patch.object(chat_server, "DATA_DIR", tmp),
            mock.patch.object(chat_server, "BACKEND", "mock"),
            mock.patch("motion_agent.app.product_api.media_allowed", return_value=True),
            mock.patch.object(chat_server, "answer_question", side_effect=lambda q, **kw: SimpleNamespace(
                answer="SAY: Keep your trail hip back.\nSCREEN:\n- Pelvis drift: 8 cm", evidence=['{"d": 0.08}'], error=None,
                voice=kw.get("voice"))),
            mock.patch.dict("os.environ", {
                "CEREBEL_VOICE_ENABLED": "1", "CEREBEL_VOICE_PLAN": "all", "CEREBEL_VOICE_PROVIDER": "mock",
                "CEREBEL_VOICE_MOCK_SCRIPT": "How are my hips?", "CEREBEL_VOICE_END_SILENCE_MS": "300",
            }),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server.Handler)
        self.server.operator = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "ws://127.0.0.1:%d/api/voice/live" % self.server.server_address[1]

    def test_full_call_over_a_socket(self):
        sock = wslib.connect(self.url, {"Origin": "http://127.0.0.1:%d" % self.server.server_address[1]})
        sock.send_text(json.dumps({"type": "start", "video_id": "vid"}))
        self.assertEqual(json.loads(sock.recv()[1])["type"], "ready")
        tone = array.array("h", (int(8000 * math.sin(i / 5)) for i in range(800))).tobytes()
        for _ in range(8):
            sock.send_binary(tone)
        for _ in range(10):
            sock.send_binary(b"\x00\x00" * 800)
        seen, audio_turns = [], set()
        deadline = time.time() + 8
        while time.time() < deadline:
            op, payload = sock.recv()
            if op == wslib.OP_BINARY:
                audio_turns.add(struct.unpack("<I", payload[:4])[0])
                continue
            msg = json.loads(payload)
            seen.append(msg)
            if msg["type"] == "audio_end":
                break
        kinds = [m["type"] for m in seen]
        self.assertIn("user", kinds)
        coach = next(m for m in seen if m["type"] == "coach")
        self.assertEqual(coach["say"], "Keep your trail hip back.")
        self.assertEqual(coach["screen"], ["Pelvis drift: 8 cm"])
        self.assertEqual(audio_turns, {1})
        self.assertTrue(self.cs.answer_question.call_args.kwargs["voice"])
        sock.send_text(json.dumps({"type": "hangup"}))
        self.assertTrue(_wait(lambda: sock.recv() is None, timeout=3))
        events = (self.cs.SETTINGS.product_dir / "events.jsonl").read_text()
        self.assertIn("voice_call_end", events)
        self.assertNotIn("hips", events)  # no transcript in the event log

    def test_foreign_origin_is_refused(self):
        with self.assertRaises(wslib.WebSocketClosed):
            wslib.connect(self.url, {"Origin": "https://evil.example"})

    def test_without_a_speech_key_only_device_calls_start(self):
        import urllib.request

        env = {"CEREBEL_VOICE_PROVIDER": "together", "CEREBEL_VOICE_API_KEY": "", "TOGETHER_API_KEY": "", "MOTION_AGENT_BASE_URL": ""}
        with mock.patch.dict("os.environ", env):
            status = json.loads(urllib.request.urlopen(self.url.replace("ws://", "http://").replace("live", "status")).read())
            self.assertEqual((status["enabled"], status["cloud"]), (True, False))
            cloud = wslib.connect(self.url)
            cloud.send_text(json.dumps({"type": "start", "video_id": "vid", "speech": "cloud"}))
            end = json.loads(cloud.recv()[1])
            self.assertEqual((end["type"], end["reason"]), ("end", "unavailable"))
            device = wslib.connect(self.url)
            device.send_text(json.dumps({"type": "start", "video_id": "vid", "speech": "device"}))
            self.assertEqual(json.loads(device.recv()[1])["speech"], "device")
            device.send_text(json.dumps({"type": "say", "text": "How are my hips?"}))
            seen = []
            while not any(m["type"] == "audio_end" for m in seen):
                op, payload = device.recv()
                self.assertEqual(op, wslib.OP_TEXT)  # no audio in a device call
                seen.append(json.loads(payload))
            self.assertEqual(next(m for m in seen if m["type"] == "coach")["say"], "Keep your trail hip back.")
            device.send_text(json.dumps({"type": "hangup"}))

    def test_disabled_by_default(self):
        with mock.patch.dict("os.environ", {"CEREBEL_VOICE_ENABLED": "0"}):
            with self.assertRaises(wslib.WebSocketClosed):
                wslib.connect(self.url)
            import urllib.request

            status = json.loads(urllib.request.urlopen(self.url.replace("ws://", "http://").replace("live", "status")).read())
            self.assertEqual(status, {"enabled": False})


if __name__ == "__main__":
    unittest.main()
