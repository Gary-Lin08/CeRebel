"""Streaming speech services for the live voice coach.

Audio in: 16 kHz mono PCM16 from the browser. Audio out: 24 kHz mono PCM16.
Providers are chosen with CEREBEL_VOICE_PROVIDER:
  together   realtime transcription + streaming synthesis on Together AI (United States), the
             same company that already serves the coach model
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
from urllib.parse import quote

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


# --------------------------------------------------------------------------- Together AI


def together_key() -> str | None:
    """CEREBEL_VOICE_API_KEY, else TOGETHER_API_KEY, else the coach key when the coach runs on Together."""
    for name in ("CEREBEL_VOICE_API_KEY", "TOGETHER_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    if "together" in (os.environ.get("MOTION_AGENT_BASE_URL") or "").lower():
        value = (os.environ.get("MOTION_AGENT_API_KEY") or "").strip()
        if value:
            return value
    return None


class TogetherAsr(AsrStream):
    """Realtime transcription: base64 PCM in, interim deltas and completed segments out.
    The service's own voice-activity detection ends each segment after a pause."""

    def __init__(self, base: str, key: str, on_partial: OnText, on_final: OnText, on_error: OnText) -> None:
        self.on_partial, self.on_final, self.on_error = on_partial, on_final, on_error
        model = _env("CEREBEL_VOICE_ASR_MODEL", "openai/whisper-large-v3")
        url = (
            f"{base}/v1/realtime?intent=transcription&model={quote(model, safe='')}"
            "&input_audio_format=pcm_s16le_16000"
        )
        self.sock = wslib.connect(url, {"Authorization": "Bearer " + key, "OpenAI-Beta": "realtime=v1"})
        self._closed = False
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._read, name="voice-asr", daemon=True)
        self._thread.start()
        if not self._started.wait(8):
            self.close()
            raise wslib.WebSocketClosed("transcription did not start")
        self.sock.send_text(json.dumps({
            "type": "transcription_session.updated",
            "session": {"turn_detection": {
                "type": "server_vad",
                "threshold": float(_env("CEREBEL_VOICE_VAD_THRESHOLD", "0.3")),
                "min_silence_duration_ms": int(_env("CEREBEL_VOICE_END_SILENCE_MS", "600")),
                "min_speech_duration_ms": 250,
                # The default (5 s) would cut a longer question into several turns.
                "max_speech_duration_s": 30,
                "speech_pad_ms": 250,
            }},
        }))

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
            kind = ev.get("type")
            if kind == "session.created":
                self._started.set()
            elif kind == "conversation.item.input_audio_transcription.delta":
                text = str(ev.get("delta") or "").strip()  # each delta replaces the previous one
                if text:
                    self.on_partial(text)
            elif kind == "conversation.item.input_audio_transcription.completed":
                text = str(ev.get("transcript") or "").strip()
                if text:
                    self.on_final(text)
            elif kind == "conversation.item.input_audio_transcription.failed":
                LOG.warning("transcription segment failed: %s", str(ev.get("error"))[:200])
            elif kind == "error":
                detail = ev.get("error") if isinstance(ev.get("error"), dict) else ev
                LOG.warning("transcription error: %s", str(detail.get("message") or detail)[:200])
                self.on_error("transcription failed")
                break
        if not self._closed:
            self.on_error("transcription stream closed")

    def send_audio(self, pcm: bytes) -> None:
        if not self._closed:
            self.sock.send_text(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode("ascii")}))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.sock.close()


def _strip_wav_header(chunk: bytes) -> bytes:
    """The first PCM chunk can arrive with a WAV header in front of the samples."""
    if not chunk.startswith(b"RIFF"):
        return chunk
    at = chunk.find(b"data")
    return chunk[at + 8 :] if at >= 0 else chunk[44:]


class TogetherTts(Synthesizer):
    """Streaming synthesis over one WebSocket; each sentence is its own context, synthesized on commit."""

    def __init__(self, base: str, key: str) -> None:
        self.key = key
        model = _env("CEREBEL_VOICE_TTS_MODEL", "hexgrad/Kokoro-82M")
        voice = _env("CEREBEL_VOICE_TTS_VOICE", "af_heart")
        self.url = (
            f"{base}/v1/audio/speech/websocket?model={quote(model, safe='')}&voice={quote(voice, safe='+()')}"
            f"&response_format=pcm&sample_rate={OUT_RATE}&segment=never"
        )
        self.first_audio_timeout = float(_env("CEREBEL_VOICE_TTS_TIMEOUT_S", "5"))
        self.sock: wslib.WebSocket | None = None

    def _open(self) -> wslib.WebSocket:
        sock = wslib.connect(self.url, {"Authorization": "Bearer " + self.key})
        sock.sock.settimeout(self.first_audio_timeout)
        return sock

    def warm(self) -> None:
        try:
            if self.sock is None or self.sock.closed:
                self.sock = self._open()
        except (OSError, ValueError, wslib.WebSocketClosed) as exc:
            LOG.warning("synthesis warm-up failed: %s", exc)
            self.sock = None

    def synthesize(self, text: str, on_audio: OnAudio, cancel: threading.Event) -> bool:
        context = uuid.uuid4().hex[:16]
        try:
            if self.sock is None or self.sock.closed:
                self.sock = self._open()
            sock = self.sock
            sock.send_text(json.dumps({"type": "input_text_buffer.append", "text": text, "context_id": context}))
            sock.send_text(json.dumps({"type": "input_text_buffer.commit", "context_id": context}))
            first = True
            while True:
                if cancel.is_set():
                    # A fresh socket next time is simpler than draining this sentence's audio.
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
                if ev.get("context_id") not in (None, context):
                    continue  # audio still arriving for an earlier sentence
                kind = ev.get("type")
                if kind == "conversation.item.audio_output.delta" and ev.get("delta"):
                    chunk = base64.b64decode(ev["delta"])
                    if first:
                        chunk, first = _strip_wav_header(chunk), False
                    if chunk:
                        on_audio(chunk[: len(chunk) - len(chunk) % 2])
                elif kind == "conversation.item.audio_output.done":
                    return True
                elif kind in ("conversation.item.tts.failed", "error"):
                    LOG.warning("synthesis failed: %s", str(ev.get("error") or ev)[:200])
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


class TogetherProvider(SpeechProvider):
    name = "together"

    def __init__(self) -> None:
        key = together_key()
        if not key:
            raise RuntimeError("no speech API key: set CEREBEL_VOICE_API_KEY")
        self.key = key
        self.base = _env("CEREBEL_VOICE_WS_BASE", "wss://api.together.ai").rstrip("/")

    def open_asr(self, on_partial: OnText, on_final: OnText, on_error: OnText) -> AsrStream:
        return TogetherAsr(self.base, self.key, on_partial, on_final, on_error)

    def open_tts(self) -> Synthesizer:
        return TogetherTts(self.base, self.key)


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


def provider_name() -> str:
    return _env("CEREBEL_VOICE_PROVIDER", "together").lower()


def provider_ready() -> bool:
    """Whether a call could start: the provider is known and has its key."""
    name = provider_name()
    return name == "mock" or (name == "together" and bool(together_key()))


def build_provider(name: str | None = None) -> SpeechProvider:
    name = (name or provider_name()).lower()
    if name == "mock":
        return MockProvider()
    if name == "together":
        return TogetherProvider()
    raise ValueError(f"unknown CEREBEL_VOICE_PROVIDER: {name}")
