"""Place a live voice call against a running server and time each stage.

    python motion_agent/scripts/voice_probe.py ws://127.0.0.1:8192/api/voice/live \
        --say "What's the biggest thing I should fix?" --say "How fast were my hips turning at impact?"

Each question is spoken at real-time pace (16 kHz PCM16), followed by silence. Questions come
from --wav files (16 kHz mono) or are synthesized with --say through the configured Model Studio
key (local testing only). --barge-in sends the next question while the coach is still talking.
Prints one JSON line per turn: ms from the end of speech to the transcript, the first audio
(the opening line), the reply text and the first audio of the reply.
"""

from __future__ import annotations

import argparse
import array
import base64
import json
import struct
import sys
import threading
import time
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from motion_agent.app import ws as wslib  # noqa: E402

FRAME = 1600  # 100 ms


def synthesize(text: str) -> bytes:
    from motion_agent.llm.qwen_client import _load_api_key

    body = json.dumps({"model": "qwen3-tts-flash", "input": {"text": text, "voice": "Cherry", "language_type": "English"}}).encode()
    req = urllib.request.Request(
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        data=body,
        headers={"Authorization": "Bearer " + _load_api_key(), "Content-Type": "application/json", "X-DashScope-SSE": "enable"},
    )
    pcm = bytearray()
    with urllib.request.urlopen(req, timeout=30) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                data = ((json.loads(line[5:]).get("output") or {}).get("audio") or {}).get("data")
                if data:
                    pcm += base64.b64decode(data)
    src = array.array("h", bytes(pcm))  # 24 kHz -> 16 kHz
    return array.array("h", (src[int(i * 1.5)] for i in range(int(len(src) / 1.5)))).tobytes()


def read_wav(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path}: need 16 kHz mono 16-bit")
        return w.readframes(w.getnframes())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--say", action="append", default=[])
    ap.add_argument("--wav", action="append", default=[])
    ap.add_argument("--token", default="")
    ap.add_argument("--video-id", default=None)
    ap.add_argument("--barge-in", action="store_true", help="ask the next question 1 s into each reply")
    args = ap.parse_args()
    clips = [read_wav(p) for p in args.wav] + [synthesize(t) for t in args.say]
    if not clips:
        raise SystemExit("give --say or --wav")

    sock = wslib.connect(args.url)
    sock.send_text(json.dumps({"type": "start", "token": args.token, "video_id": args.video_id}))
    events: list[tuple[float, dict]] = []
    audio: list[tuple[float, int]] = []
    done = threading.Event()

    def reader() -> None:
        while True:
            msg = sock.recv()
            if msg is None:
                break
            op, payload = msg
            now = time.time()
            if op == wslib.OP_BINARY:
                audio.append((now, struct.unpack("<I", payload[:4])[0]))
            else:
                events.append((now, json.loads(payload)))
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    while not any(e[1].get("type") in ("ready", "end") for e in events):
        time.sleep(0.02)
    if events[-1][1].get("type") == "end":
        print(json.dumps(events[-1][1]))
        return

    def speak(pcm: bytes) -> float:
        for i in range(0, len(pcm), FRAME * 2):
            sock.send_binary(pcm[i : i + FRAME * 2])
            time.sleep(0.1)
        return time.time()

    def silence(seconds: float) -> None:
        for _ in range(int(seconds * 10)):
            sock.send_binary(b"\x00\x00" * FRAME)
            time.sleep(0.1)

    interrupted_at: dict[int, float] = {}
    for n, clip in enumerate(clips, start=1):
        if done.is_set():
            break
        try:
            spoke_at = speak(clip)
            silence(1.0)
        except wslib.WebSocketClosed:
            break
        deadline = time.time() + 45
        while time.time() < deadline:
            ends = [e for e in events if e[1].get("type") == "audio_end" and e[1].get("turn") == n]
            if ends or done.is_set():
                break
            if args.barge_in and n < len(clips) and any(t == n for _, t in audio):
                first = min(t for t, k in audio if k == n)
                if time.time() - first > 1.0:
                    sock.send_text(json.dumps({"type": "interrupt"}))
                    interrupted_at[n] = time.time()
                    break
            time.sleep(0.05)

        def first(kind: str) -> float | None:
            hits = [t for t, e in events if e.get("type") == kind and e.get("turn") == n]
            return round((min(hits) - spoke_at) * 1000) if hits else None

        turn_audio = [t for t, k in audio if k == n]
        coach = next((e for _, e in events if e.get("type") == "coach" and e.get("turn") == n), {})
        coach_at = first("coach")
        answer_audio = [t for t in turn_audio if coach_at is not None and t - spoke_at >= coach_at / 1000]
        if n - 1 in interrupted_at:
            # Audio of the interrupted turn that still arrived after the interrupt (should be ~0).
            late = [t for t, k in audio if k == n - 1 and t > interrupted_at[n - 1] + 0.3]
            print(json.dumps({"turn": n - 1, "late_audio_chunks_after_interrupt": len(late)}))
        print(json.dumps({
            "turn": n,
            "heard": next((e.get("text") for _, e in events if e.get("type") == "user" and e.get("turn") == n), None),
            "ms_transcript": first("user"),
            "ms_first_audio": round((min(turn_audio) - spoke_at) * 1000) if turn_audio else None,
            "ms_reply_text": coach_at,
            "ms_reply_audio": round((min(answer_audio) - spoke_at) * 1000) if answer_audio else None,
            "instant": coach.get("instant"),
            "say": coach.get("say"),
            "screen": coach.get("screen"),
        }), flush=True)
        try:
            silence(0.5)
        except wslib.WebSocketClosed:
            break
    try:
        sock.send_text(json.dumps({"type": "hangup"}))
    except wslib.WebSocketClosed:
        pass
    done.wait(5)
    tail = [e for _, e in events if e.get("type") == "end"]
    if tail:
        print(json.dumps(tail[-1]))


if __name__ == "__main__":
    main()
