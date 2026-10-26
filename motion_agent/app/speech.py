"""Streaming speech services for the live voice coach.

Audio in: 16 kHz mono PCM16 from the browser. Audio out: 24 kHz mono PCM16.
Providers are chosen with CEREBEL_VOICE_PROVIDER:
  dashscope  streaming recognition + realtime synthesis on Model Studio (local demo key)
  mock       offline stand-ins for tests and UI checks
Nothing here is shown to users; provider names and errors stay in server logs.
"""

from __future__ import annotations

import array
import base64
import json
import logging
import math
import os
import threading
import time
import uuid
from typing import Callable

from motion_agent.app import ws as wslib

LOG = logging.getLogger("motion_agent.speech")

IN_RATE = 16000
OUT_RATE = 24000

OnText = Callable[[str], None]
OnAudio = Callable[[bytes], None]


class AsrStream:
    """One recognition stream per call. Callbacks run on the stream's reader thread."""

    def send_audio(self, pcm: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class Synthesizer:
    """Turns one sentence at a time into PCM. Used from one thread."""

    def synthesize(self, text: str, on_audio: OnAudio, cancel: threading.Event) -> bool:
        raise NotImplementedError

    def warm(self) -> None:
        """Open the connection ahead of the first sentence, if the service has one."""

    def close(self) -> None:
        pass


class SpeechProvider:
    name = "base"

    def open_asr(self, on_partial: OnText, on_final: OnText, on_error: OnText) -> AsrStream:
        raise NotImplementedError

    def open_tts(self) -> Synthesizer:
        raise NotImplementedError


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


# --------------------------------------------------------------------------- DashScope


class DashScopeAsr(AsrStream):
    """fun-asr-realtime over the inference WebSocket (run-task / binary audio / finish-task)."""

    def __init__(self, base: str, key: str, on_partial: OnText, on_final: OnText, on_error: OnText) -> None:
        self.on_partial, self.on_final, self.on_error = on_partial, on_final, on_error
        self.task_id = uuid.uuid4().hex
        self.sock = wslib.connect(base + "/api-ws/v1/inference", {"Authorization": "Bearer " + key})
        self.sock.send_text(json.dumps({
            "header": {"action": "run-task", "task_id": self.task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio", "task": "asr", "function": "recognition",
                "model": _env("CEREBEL_VOICE_ASR_MODEL", "fun-asr-realtime"),
                "parameters": {
                    "format": "pcm",
                    "sample_rate": IN_RATE,
                    "language_hints": ["en"],
                    "max_sentence_silence": int(_env("CEREBEL_VOICE_END_SILENCE_MS", "600")),
                    "heartbeat": True,
                },
                "input": {},
            },
        }))
        self._started = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._read, name="voice-asr", daemon=True)
        self._thread.start()
        if not self._started.wait(8):
            self.close()
            raise wslib.WebSocketClosed("recognition did not start")

    def _read(self) -> None:
        while True:
            msg = self.sock.recv()
            if msg is None:
                break
            op, payload = msg
            if op != wslib.OP_TEXT:
                continue
            try:
                ev = json.loads(payload)
            except ValueError:
                continue
            event = (ev.get("header") or {}).get("event")
            if event == "task-started":
                self._started.set()
            elif event == "result-generated":
                sentence = ((ev.get("payload") or {}).get("output") or {}).get("sentence") or {}
                if sentence.get("heartbeat"):
                    continue
                text = str(sentence.get("text") or "").strip()
                if not text:
                    continue
                (self.on_final if sentence.get("sentence_end") else self.on_partial)(text)
            elif event == "task-failed":
                LOG.warning("recognition failed: %s", (ev.get("header") or {}).get("error_message"))
                self.on_error("recognition failed")
                break
            elif event == "task-finished":
                break
        if not self._closed:
            self.on_error("recognition stream closed")

    def send_audio(self, pcm: bytes) -> None:
        if not self._closed:
            self.sock.send_binary(pcm)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.send_text(json.dumps({
                "header": {"action": "finish-task", "task_id": self.task_id, "streaming": "duplex"},
                "payload": {"input": {}},
            }))
        except (OSError, wslib.WebSocketClosed):
            pass
        self.sock.close()


class DashScopeTts(Synthesizer):
    """Realtime synthesis over one WebSocket, reused sentence after sentence (commit mode)."""

    def __init__(self, base: str, key: str) -> None:
        self.base, self.key = base, key
        self.model = _env("CEREBEL_VOICE_TTS_MODEL", "qwen3-tts-flash-realtime")
        self.voice = _env("CEREBEL_VOICE_TTS_VOICE", "Ethan")
        self.first_audio_timeout = float(_env("CEREBEL_VOICE_TTS_TIMEOUT_S", "5"))
        self.sock: wslib.WebSocket | None = None

    def _open(self) -> wslib.WebSocket:
        sock = wslib.connect(f"{self.base}/api-ws/v1/realtime?model={self.model}", {"Authorization": "Bearer " + self.key})
        sock.sock.settimeout(self.first_audio_timeout)
        sock.recv()  # session.created
        sock.send_text(json.dumps({
            "event_id": uuid.uuid4().hex,
            "type": "session.update",
            "session": {
                "voice": self.voice,
                "response_format": "pcm",
                "sample_rate": OUT_RATE,
                "mode": "commit",
                "language_type": "English",
            },
        }))
        return sock

    def warm(self) -> None:
        try:
            if self.sock is None or self.sock.closed:
                self.sock = self._open()
        except (OSError, ValueError, wslib.WebSocketClosed) as exc:
            LOG.warning("synthesis warm-up failed: %s", exc)
            self.sock = None

    def synthesize(self, text: str, on_audio: OnAudio, cancel: threading.Event) -> bool:
        try:
            if self.sock is None or self.sock.closed:
                self.sock = self._open()
            sock = self.sock
            for kind, extra in (("input_text_buffer.append", {"text": text}), ("input_text_buffer.commit", {})):
                sock.send_text(json.dumps({"event_id": uuid.uuid4().hex, "type": kind, **extra}))
            while True:
                if cancel.is_set():
                    # Abandon the rest of this response: a fresh socket is cheaper than draining it.
                    self.close()
                    return False
                msg = sock.recv()
                if msg is None:
                    self.sock = None
                    return False
                op, payload = msg
                if op != wslib.OP_TEXT:
                    continue
                ev = json.loads(payload)
                kind = ev.get("type")
                if kind == "response.audio.delta" and ev.get("delta"):
                    on_audio(base64.b64decode(ev["delta"]))
                elif kind == "response.done":
                    return True
                elif kind == "error":
                    LOG.warning("synthesis error: %s", str(ev.get("error"))[:200])
                    self.close()
                    return False
        except (OSError, ValueError, wslib.WebSocketClosed) as exc:
            LOG.warning("synthesis failed: %s", exc)
            self.close()
            return False

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
        self.sock = None


class DashScopeProvider(SpeechProvider):
    name = "dashscope"

    def __init__(self) -> None:
        from motion_agent.llm.qwen_client import _load_api_key

        self.key = os.environ.get("CEREBEL_VOICE_API_KEY") or _load_api_key()
        self.base = _env("CEREBEL_VOICE_WS_BASE", "wss://dashscope.aliyuncs.com").rstrip("/")

    def open_asr(self, on_partial: OnText, on_final: OnText, on_error: OnText) -> AsrStream:
        return DashScopeAsr(self.base, self.key, on_partial, on_final, on_error)

    def open_tts(self) -> Synthesizer:
        return DashScopeTts(self.base, self.key)


# --------------------------------------------------------------------------- mock


def _rms(pcm: bytes) -> float:
    samples = array.array("h", pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0


class MockAsr(AsrStream):
    """Speech = audio louder than a threshold; a pause after speech ends the sentence.

    Each finished sentence yields the next line of CEREBEL_VOICE_MOCK_SCRIPT ('|' separated).
    """

    def __init__(self, on_partial: OnText, on_final: OnText, on_error: OnText, script: list[str]) -> None:
        self.on_partial, self.on_final = on_partial, on_final
        self.script = script or ["What's the biggest thing I should fix?"]
        self.index = 0
        self.speech_ms = 0.0
        self.silence_ms = 0.0
        self.end_ms = float(_env("CEREBEL_VOICE_END_SILENCE_MS", "600"))

    def send_audio(self, pcm: bytes) -> None:
        ms = len(pcm) / 2 / IN_RATE * 1000
        if _rms(pcm) > 0.02:
            self.speech_ms += ms
            self.silence_ms = 0.0
            if self.speech_ms >= 200 and int(self.speech_ms) % 400 < ms:
                self.on_partial(self.script[self.index % len(self.script)].split(" ")[0] + "…")
            return
        if self.speech_ms >= 200:
            self.silence_ms += ms
            if self.silence_ms >= self.end_ms:
                text = self.script[self.index % len(self.script)]
                self.index += 1
                self.speech_ms = self.silence_ms = 0.0
                self.on_final(text)
        else:
            self.speech_ms = 0.0

    def close(self) -> None:
        pass


class MockTts(Synthesizer):
    """A soft tone, 70 ms per word, delivered in 100 ms chunks at about real time / 4."""

    def synthesize(self, text: str, on_audio: OnAudio, cancel: threading.Event) -> bool:
        words = max(1, len(text.split()))
        total = int(OUT_RATE * min(8.0, 0.07 * words + 0.2))
        chunk = OUT_RATE // 10
        for start in range(0, total, chunk):
            if cancel.is_set():
                return False
            n = min(chunk, total - start)
            samples = array.array("h", (int(2500 * math.sin(2 * math.pi * 220 * (start + i) / OUT_RATE)) for i in range(n)))
            on_audio(samples.tobytes())
            time.sleep(0.025)
        return True


class MockProvider(SpeechProvider):
    name = "mock"

    def __init__(self) -> None:
        raw = os.environ.get("CEREBEL_VOICE_MOCK_SCRIPT", "")
        self.script = [s.strip() for s in raw.split("|") if s.strip()]

    def open_asr(self, on_partial: OnText, on_final: OnText, on_error: OnText) -> AsrStream:
        return MockAsr(on_partial, on_final, on_error, self.script)

    def open_tts(self) -> Synthesizer:
        return MockTts()


def build_provider(name: str | None = None) -> SpeechProvider:
    name = (name or _env("CEREBEL_VOICE_PROVIDER", "dashscope")).lower()
    if name == "mock":
        return MockProvider()
    if name == "dashscope":
        return DashScopeProvider()
    raise ValueError(f"unknown CEREBEL_VOICE_PROVIDER: {name}")
