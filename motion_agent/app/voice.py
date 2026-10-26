"""Live voice coach: one WebSocket call = streaming recognition -> coach -> streaming speech.

Browser -> server
  {"type": "start", "token": "...", "video_id": "..."}    first message, within 10 s
  binary frames: 16 kHz mono PCM16 microphone audio
  {"type": "interrupt"}   the user started talking over the coach
  {"type": "hangup"}
Server -> browser
  {"type": "ready", "seconds": n}        call is live, n = most seconds this call may last
  {"type": "partial", "text"}            live caption of what the user is saying
  {"type": "user", "turn", "text"}       final transcript of a turn
  {"type": "state", "turn", "state"}     thinking | speaking
  {"type": "coach", "turn", "say", "screen": [...], "instant"}
  binary frames: 4-byte little-endian turn id + 24 kHz mono PCM16
  {"type": "audio_end", "turn"}
  {"type": "end", "reason", "message"}   then the socket closes

The coach answers with the same tool-grounded agent as the text chat, in voice mode: the spoken
part has no numbers; measured numbers go to the screen and must appear in this turn's tool output.
Audio is never written to disk and transcripts live only in this call's memory.
"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from motion_agent.app import ws as wslib
from motion_agent.app.speech import SpeechProvider, Synthesizer

LOG = logging.getLogger("motion_agent.voice")

ACK_PHRASES = ("Let me look at that.", "Okay, let me look at that.", "Good question. Let me look at that.")
MAX_SPOKEN_SENTENCES = 3
MAX_SCREEN_LINES = 4
_USAGE_LOCK = threading.Lock()
_ACK_CACHE: dict[tuple[str, str], bytes] = {}
_ACK_LOCK = threading.Lock()


# --------------------------------------------------------------------------- settings / usage


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default


def voice_enabled() -> bool:
    return os.environ.get("CEREBEL_VOICE_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


def voice_plan() -> str:
    """trial: free accounts get a one-time trial, Pro gets a monthly allowance. pro: Pro only. all: anyone."""
    plan = os.environ.get("CEREBEL_VOICE_PLAN", "trial").strip().lower()
    return plan if plan in ("trial", "pro", "all") else "trial"


def _usage(product_dir: Path) -> dict[str, Any]:
    path = product_dir / "voice_usage.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def voice_allowance(product_dir: Path, user: dict[str, Any] | None, pro: bool) -> dict[str, Any]:
    """How long the next call may last: {"seconds", "kind": trial|pro|open, "need": None|signin|upgrade}."""
    max_call = _env_int("CEREBEL_VOICE_MAX_CALL_S", 600)
    plan = voice_plan()
    if plan == "all":
        return {"seconds": max_call, "kind": "open", "need": None}
    if not user:
        return {"seconds": 0, "kind": "", "need": "signin"}
    row = (_usage(product_dir).get("users") or {}).get(str(user["user_id"])) or {}
    if pro:
        used = int(row.get("month_s", 0)) if row.get("month") == _month() else 0
        left = _env_int("CEREBEL_VOICE_PRO_MONTHLY_S", 3600) - used
        kind = "pro"
    elif plan == "trial":
        left = _env_int("CEREBEL_VOICE_TRIAL_S", 180) - int(row.get("trial_s", 0))
        kind = "trial"
    else:
        return {"seconds": 0, "kind": "", "need": "upgrade"}
    left = max(0, min(left, max_call))
    if left < 10:
        return {"seconds": 0, "kind": kind, "need": "upgrade"}
    return {"seconds": left, "kind": kind, "need": None}


def record_voice_seconds(product_dir: Path, user_id: str, kind: str, seconds: float) -> None:
    if kind not in ("trial", "pro") or seconds <= 0:
        return
    with _USAGE_LOCK:
        data = _usage(product_dir)
        users = data.setdefault("users", {})
        row = users.setdefault(str(user_id), {})
        if kind == "trial":
            row["trial_s"] = int(row.get("trial_s", 0)) + int(round(seconds))
        else:
            if row.get("month") != _month():
                row["month"], row["month_s"] = _month(), 0
            row["month_s"] = int(row.get("month_s", 0)) + int(round(seconds))
        product_dir.mkdir(parents=True, exist_ok=True)
        path = product_dir / "voice_usage.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)


# --------------------------------------------------------------------------- reply text

_DIGIT = re.compile(r"\d")
_SPELLED_MEASURE = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|half)\b[\w\s-]{0,20}?"
    r"\b(?:degrees?|centimet(?:er|re)s?|millimet(?:er|re)s?|met(?:er|re)s?|inch(?:es)?|feet|foot|mph|"
    r"miles|kilomet(?:er|re)s?|percent|seconds?|milliseconds?|newtons?|watts?|pounds?|kilos?)\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_CONVERSIONS = (1.0, 100.0, 1000.0, 0.01, 0.001, 57.29577951308232)  # m->cm, s->ms, ratio->%, rad->deg


def _clean(text: str) -> str:
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.DOTALL)  # fences, incl. an unclosed one
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`#>]+", "", text)
    text = text.replace("°", " degrees").replace("&", " and ")
    return re.sub(r"\s+", " ", text).strip()


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", _clean(text)) if s.strip()]


def _end_with_stop(sentence: str) -> str:
    """Headlines have no full stop; without one the voice runs them into the next sentence."""
    sentence = sentence.rstrip(" :;,")
    return sentence if sentence[-1:] in ".!?" else sentence + "."


# A precomputed answer fits "what's wrong with my hips?" but not "how fast were my hips turning?".
_SPECIFIC_QUESTION = re.compile(
    r"\b(how (fast|much|far|long|high|many)|speed|angle|degrees?|when|why|compare|rule|drill|exercise|"
    r"what about|club|ball|score|distance)\b",
    re.IGNORECASE,
)


def match_voice_brief(question: str, briefs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Like coach_briefs.match_brief, but spoken questions that ask for something specific go to
    the coach instead of the keyword fallbacks."""
    from motion_agent.app.coach_briefs import match_brief

    q = (question or "").strip().lower().rstrip("?.! ")
    for brief in briefs:
        bq = str(brief.get("question") or "").strip().lower().rstrip("?.! ")
        if bq and (q == bq or bq in q):
            return brief
    if _SPECIFIC_QUESTION.search(q):
        return None
    return match_brief(question, briefs)


def has_measurement(sentence: str) -> bool:
    return bool(_DIGIT.search(sentence) or _SPELLED_MEASURE.search(sentence))


def plan_reply(answer: str) -> tuple[list[str], list[str]]:
    """Split a voice-mode answer into (sentences to speak, lines for the screen)."""
    text = re.sub(r"```.*?(```|$)", " ", answer or "", flags=re.DOTALL)
    say, screen_text = text, ""
    match = re.search(r"\bSCREEN\s*:", text, flags=re.IGNORECASE)
    if match:
        say, screen_text = text[: match.start()], text[match.end() :]
    say = re.sub(r"^\s*SAY\s*:", "", say.strip(), flags=re.IGNORECASE)
    screen: list[str] = []
    for line in screen_text.splitlines():
        line = _clean(re.sub(r"^\s*[-•*]\s*", "", line))
        if line:
            screen.append(line)
    spoken: list[str] = []
    for sentence in _sentences(say):
        if has_measurement(sentence):
            screen.append(sentence)  # numbers are read on the screen, not out loud
        elif len(spoken) < MAX_SPOKEN_SENTENCES:
            spoken.append(sentence)
    return spoken, screen[:MAX_SCREEN_LINES]


def brief_reply(brief: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Spoken version of a precomputed instant answer (no model call)."""
    spoken: list[str] = []
    screen: list[str] = []
    cue: str | None = None
    for para in re.split(r"\n+", str(brief.get("answer") or "")):
        para = para.strip()
        if not para:
            continue
        if para.lower().startswith("measured:"):
            screen.append(_clean(para.split(":", 1)[1]))
            continue
        for sentence in _sentences(para.lstrip("•").strip()):
            if has_measurement(sentence):
                screen.append(sentence)
            elif sentence.lower().startswith(("what to feel", "hold this feel")) and cue is None:
                cue = sentence
            elif len(spoken) < 2:
                spoken.append(_end_with_stop(sentence))
    if cue:
        spoken.append(cue)
    return spoken[:MAX_SPOKEN_SENTENCES], screen[:MAX_SCREEN_LINES]


_STOP = {"your", "with", "from", "after", "before", "through", "toward", "towards", "into", "that", "this",
         "they", "there", "what", "when", "were", "have", "does", "about", "should", "swing", "ball", "turn",
         "impact", "address"}


def _topic_words(text: str) -> set[str]:
    words = {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 4 and w not in _STOP}
    return words | {w[:-1] for w in words if w.endswith("s")}


def topic_metrics(text: str, findings: list[dict[str, Any]]) -> list[str]:
    """Measured values of the swing findings this turn talks about, as shown in the report."""
    words = _topic_words(text)
    lines = []
    for finding in findings or []:
        measured = next(
            (p.split(":", 1)[1] for p in str(finding.get("answer") or "").split("\n") if p.lower().startswith("measured:")),
            "",
        )
        topic = str(finding.get("id") or "") + " " + measured  # titles are too broad ("hips and shoulders")
        if measured.strip() and words & _topic_words(topic.replace("-", " ")):
            lines.append(_clean(measured))
    return lines


_HEDGE = re.compile(r"~|≈|\b(about|around|approx\w*|roughly|estimat\w*|inferred|derived|calculated|likely)\b", re.IGNORECASE)


def screen_line_problem(line: str, evidence: str) -> str | None:
    """Why a coach-written screen line should not be shown, or None. The screen is for measured
    values only: no hedged or worked-out numbers, nothing missing from this turn's tool output."""
    if not _DIGIT.search(line):
        return "no number"
    if _HEDGE.search(line):
        return "estimate"
    if ungrounded_numbers(line, evidence):
        return "not in tool output"
    return None


def ungrounded_numbers(text: str, evidence: str) -> list[str]:
    """Numbers in `text` (2+ digits or with decimals) that no tool output contains, allowing for
    rounding to the shown precision and the usual unit conversions."""
    values = [abs(float(v)) for v in _NUMBER.findall(evidence or "")]
    missing = []
    for token in _NUMBER.findall(text or ""):
        n = abs(float(token))
        places = len(token.split(".")[1]) if "." in token else 0
        if places == 0 and n < 10:
            continue  # small counts and labels: "3 swings", "P4"
        # "about 390" for 394.3: a whole number ending in zero may be rounded to the nearest ten.
        tol = 5.0 if places == 0 and n >= 100 and n % 10 == 0 else None
        found = any(
            round(v * c, places) == n or (tol is not None and abs(v * c - n) <= tol)
            for v in values
            for c in _CONVERSIONS
        )
        if not found:
            missing.append(token)
    return missing


# --------------------------------------------------------------------------- call


class VoiceCall:
    """Runs on the request thread; recognition callbacks and coach turns run on their own threads."""

    def __init__(
        self,
        sock: wslib.WebSocket,
        provider: SpeechProvider,
        *,
        answer_fn: Callable[[str, list[dict[str, Any]]], Any],
        seconds: int,
        take_question: Callable[[], tuple[bool, str]] | None = None,
        briefs_fn: Callable[[], list[dict[str, Any]]] | None = None,
        idle_seconds: int | None = None,
        log_event: Callable[..., None] | None = None,
    ) -> None:
        self.sock = sock
        self.provider = provider
        self.answer_fn = answer_fn
        self.seconds = max(1, int(seconds))
        self.take_question = take_question or (lambda: (True, ""))
        self.briefs: list[dict[str, Any]] = []
        self.findings: list[dict[str, Any]] = []
        self._briefs_fn = briefs_fn
        self.idle_seconds = idle_seconds or _env_int("CEREBEL_VOICE_IDLE_S", 90)
        self.log_event = log_event or (lambda *a, **k: None)
        self.history: list[dict[str, str]] = []
        self.lock = threading.Lock()
        self.tts_lock = threading.Lock()
        self.turn_id = 0
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.ended = threading.Event()
        self.started_at = time.time()
        self.last_activity = time.time()
        self.stats = {"turns": 0, "interrupted": 0, "instant": 0, "ungrounded": 0, "tts_failed": 0, "first_audio_ms": [], "answer_audio_ms": []}
        self.end_reason = "hangup"
        self.asr = None
        self.tts: Synthesizer | None = None

    # ---------------------------------------------------------------- plumbing
    def _send(self, payload: dict[str, Any]) -> None:
        try:
            self.sock.send_text(json.dumps(payload))
        except (OSError, wslib.WebSocketClosed):
            self.ended.set()

    def _send_audio(self, turn: int, pcm: bytes) -> None:
        try:
            self.sock.send_binary(struct.pack("<I", turn) + pcm)
        except (OSError, wslib.WebSocketClosed):
            self.ended.set()

    def end(self, reason: str, message: str = "") -> None:
        if self.ended.is_set():
            return
        self.end_reason = reason
        self.cancel.set()
        self._send({"type": "end", "reason": reason, "message": message})
        self.ended.set()
        self.sock.close()

    # ---------------------------------------------------------------- lifecycle
    def run(self) -> None:
        try:
            self.tts = self.provider.open_tts()
            self.asr = self.provider.open_asr(self._on_partial, self._on_final, self._on_asr_error)
        except Exception:  # noqa: BLE001
            LOG.exception("voice services unavailable")
            self.end("error", "Live coaching isn't available right now. You can keep typing your questions.")
            return
        threading.Thread(target=self._prepare, name="voice-prep", daemon=True).start()
        threading.Thread(target=self._watchdog, name="voice-watchdog", daemon=True).start()
        self._send({"type": "ready", "seconds": self.seconds})
        try:
            while not self.ended.is_set():
                msg = self.sock.recv()
                if msg is None:
                    break
                op, payload = msg
                if op == wslib.OP_BINARY:
                    if payload:
                        self.asr.send_audio(payload)
                    continue
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                kind = data.get("type")
                if kind == "interrupt":
                    self._interrupt()
                elif kind == "hangup":
                    self.end("hangup")
        except Exception:  # noqa: BLE001
            LOG.exception("voice call loop failed")
            self.end("error", "The call dropped. You can keep typing your questions.")
        finally:
            self.ended.set()
            self.cancel.set()
            for closer in (getattr(self.asr, "close", None), getattr(self.tts, "close", None)):
                try:
                    if closer:
                        closer()
                except Exception:  # noqa: BLE001
                    LOG.exception("closing a voice service failed")
            self.sock.close()

    def duration(self) -> float:
        return time.time() - self.started_at

    def summary(self) -> dict[str, Any]:
        def p50(key: str) -> int | None:
            values = sorted(self.stats[key])
            return values[len(values) // 2] if values else None

        return {
            "seconds": round(self.duration(), 1),
            "reason": self.end_reason,
            "turns": self.stats["turns"],
            "interrupted": self.stats["interrupted"],
            "instant": self.stats["instant"],
            "ungrounded": self.stats["ungrounded"],
            "tts_failed": self.stats["tts_failed"],
            "first_audio_ms_p50": p50("first_audio_ms"),
            "answer_audio_ms_p50": p50("answer_audio_ms"),
        }

    def _prepare(self) -> None:
        # Precomputed answers, the opening line and the speech connection are ready a moment
        # after the call starts, so the first reply does not pay for them.
        with self.tts_lock:
            if self.tts is not None:
                self.tts.warm()
        if self._briefs_fn:
            try:
                payload = self._briefs_fn() or []
                if isinstance(payload, dict):
                    self.findings = list(payload.get("findings") or [])
                    payload = payload.get("briefs") or []
                self.briefs = list(payload)
            except Exception:  # noqa: BLE001
                LOG.exception("coach briefs for the call failed")
        for phrase in ACK_PHRASES:
            _ack_audio(self.provider, phrase)

    def _watchdog(self) -> None:
        while not self.ended.wait(1.0):
            if self.duration() >= self.seconds:
                self.end("time", "That's the end of this call's time. You can keep typing your questions.")
            elif time.time() - self.last_activity > self.idle_seconds and not self._busy():
                self.end("idle", "Ending the call since it went quiet.")

    def _busy(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    # ---------------------------------------------------------------- recognition
    def _on_partial(self, text: str) -> None:
        self.last_activity = time.time()
        self._send({"type": "partial", "text": text[:300]})

    def _on_asr_error(self, _message: str) -> None:
        if not self.ended.is_set():
            self.end("error", "The call dropped. You can keep typing your questions.")

    def _on_final(self, text: str) -> None:
        text = (text or "").strip()
        self.last_activity = time.time()
        if len(re.sub(r"[^a-zA-Z]", "", text)) < 2 or text.lower().strip(" .!?") in ("uh", "um", "hmm", "mm"):
            return
        with self.lock:
            if self.ended.is_set():
                return
            if self._busy():
                self.stats["interrupted"] += 1
            self.cancel.set()
            self.turn_id += 1
            turn = self.turn_id
            self.cancel = cancel = threading.Event()
            self.stats["turns"] += 1
            self._send({"type": "user", "turn": turn, "text": text[:500]})
            self.worker = threading.Thread(target=self._turn, args=(turn, text, cancel, time.time()), name="voice-turn", daemon=True)
            self.worker.start()

    def _interrupt(self) -> None:
        with self.lock:
            if self._busy():
                self.stats["interrupted"] += 1
            self.cancel.set()

    # ---------------------------------------------------------------- one turn
    def _turn(self, turn: int, text: str, cancel: threading.Event, heard_at: float) -> None:
        first_audio = [None]

        def audio(pcm: bytes) -> None:
            if cancel.is_set():
                return
            if first_audio[0] is None:
                first_audio[0] = time.time()
            self._send_audio(turn, pcm)

        try:
            brief = match_voice_brief(text, self.briefs) if self.briefs else None
            if brief:
                self.stats["instant"] += 1
                spoken, screen = brief_reply(brief)
            else:
                ack = _ack_audio(self.provider, ACK_PHRASES[(turn - 1) % len(ACK_PHRASES)], wait=False)
                self._send({"type": "state", "turn": turn, "state": "thinking"})
                if ack:
                    audio(ack)
                allowed, message = self.take_question()
                if not allowed:
                    self._send({"type": "coach", "turn": turn, "say": message, "screen": [], "instant": True})
                    self._speak(turn, [message], cancel, audio)
                    self.end("limit", message)
                    return
                result = self.answer_fn(text, list(self.history))
                if cancel.is_set():
                    return
                if getattr(result, "error", None):
                    spoken, screen = ["Sorry, I couldn't look that up just now. Try asking again."], []
                else:
                    spoken, screen = plan_reply(getattr(result, "answer", "") or "")
                    evidence = "\n".join(getattr(result, "evidence", []) or [])
                    kept = []
                    for line in screen:
                        problem = screen_line_problem(line, evidence)
                        if problem:
                            self.stats["ungrounded"] += 1
                            LOG.info("voice: dropped a screen line (%s)", problem)
                            LOG.debug("voice: dropped %r", line)
                        else:
                            kept.append(line)
                    # The report's own measured values for this topic come first, then the coach's.
                    report = topic_metrics(text + " " + " ".join(spoken), self.findings)
                    numbers_shown = {n for line in report for n in _NUMBER.findall(line)}
                    kept = [line for line in kept if not set(_NUMBER.findall(line)) & numbers_shown]
                    screen = (report + kept)[:MAX_SCREEN_LINES]
                if not spoken:
                    spoken = ["I put the numbers on your screen."] if screen else ["Sorry, could you ask that another way?"]
            if cancel.is_set():
                return
            say = " ".join(spoken)
            self._send({"type": "coach", "turn": turn, "say": say, "screen": screen, "instant": bool(brief)})
            self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": "\n".join([say, *screen])}]
            answer_audio = [None]

            def answer(pcm: bytes) -> None:
                if answer_audio[0] is None and not cancel.is_set():
                    answer_audio[0] = time.time()
                    self.stats["answer_audio_ms"].append(int((answer_audio[0] - heard_at) * 1000))
                audio(pcm)

            self._speak(turn, spoken, cancel, answer)
        except Exception:  # noqa: BLE001
            LOG.exception("voice turn failed")
            self._send({"type": "coach", "turn": turn, "say": "Sorry, something went wrong. Try asking again.", "screen": [], "instant": True})
        finally:
            if first_audio[0] is not None:
                self.stats["first_audio_ms"].append(int((first_audio[0] - heard_at) * 1000))
            if not cancel.is_set():
                self._send({"type": "audio_end", "turn": turn})
            self.last_activity = time.time()

    def _speak(self, turn: int, sentences: list[str], cancel: threading.Event, audio: Callable[[bytes], None]) -> None:
        if cancel.is_set() or not sentences:
            return
        self._send({"type": "state", "turn": turn, "state": "speaking"})
        for sentence in sentences:
            with self.tts_lock:
                if cancel.is_set() or self.tts is None:
                    return
                if not self.tts.synthesize(sentence, audio, cancel):
                    if not cancel.is_set():
                        self.stats["tts_failed"] += 1
                    return  # the text is already on screen


def _ack_audio(provider: SpeechProvider, phrase: str, wait: bool = True) -> bytes | None:
    """The opening line, synthesized once per process and voice, then replayed instantly."""
    key = (provider.name + ":" + os.environ.get("CEREBEL_VOICE_TTS_VOICE", ""), phrase)
    if key in _ACK_CACHE or not wait:
        return _ACK_CACHE.get(key)
    with _ACK_LOCK:
        if key in _ACK_CACHE:
            return _ACK_CACHE[key]
        chunks: list[bytes] = []
        tts = provider.open_tts()
        try:
            if tts.synthesize(phrase, chunks.append, threading.Event()):
                _ACK_CACHE[key] = b"".join(chunks)
        finally:
            tts.close()
    return _ACK_CACHE.get(key)
