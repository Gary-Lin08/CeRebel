"""Cerebel AI glasses link protocol: length-prefixed JSON over TCP (default port 9080).

The glasses listen; the phone or computer connects. Each frame is a 4-byte big-endian length
followed by that many bytes of UTF-8 JSON, one object per frame, with a `type` field.

Glasses -> host:  hello {fw, battery}, key {action: photo|rec_start|rec_stop},
                  pcm {seq, sr, ch, data_b64}
Host -> glasses:  hud {mode: info|assist|fact|end, you, reply, status}, ping

This is the draft contract the iOS companion app (cerebel-ar-glasses/nega-ios) and its mock
glasses server already speak. Unknown types must be ignored on both sides so the protocol can grow.
"""

from __future__ import annotations

import json
import struct
from typing import Any

DEFAULT_PORT = 9080
# A frame larger than this is treated as a broken stream rather than buffered forever.
MAX_FRAME_BYTES = 1024 * 1024

HUD_MODES = ("info", "assist", "fact", "end")
# Field limits the companion app enforces before sending; the waveguide shows little more.
HUD_LIMITS = {"you": 80, "reply": 160, "status": 40}
KEY_ACTIONS = ("photo", "rec_start", "rec_stop")


class ProtocolError(ValueError):
    """The byte stream is not valid framed JSON."""


def encode_frame(message: dict[str, Any]) -> bytes:
    if not isinstance(message, dict) or not message.get("type"):
        raise ProtocolError("a frame is a JSON object with a type")
    raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame of {len(raw)} bytes exceeds {MAX_FRAME_BYTES}")
    return struct.pack(">I", len(raw)) + raw


class FrameDecoder:
    """Feed received bytes in any chunking; get back complete messages."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buf.extend(data)
        out: list[dict[str, Any]] = []
        while len(self._buf) >= 4:
            (length,) = struct.unpack(">I", self._buf[:4])
            if length > MAX_FRAME_BYTES:
                raise ProtocolError(f"frame length {length} exceeds {MAX_FRAME_BYTES}")
            if len(self._buf) < 4 + length:
                break
            raw = bytes(self._buf[4 : 4 + length])
            del self._buf[: 4 + length]
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProtocolError(f"bad frame: {exc}") from None
            if isinstance(message, dict):
                out.append(message)
        return out


def clip(text: str, limit: int) -> str:
    """Shorten to `limit` characters, ending with an ellipsis when something was cut."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def hud(*, mode: str = "info", you: str = "", reply: str = "", status: str = "") -> dict[str, Any]:
    if mode not in HUD_MODES:
        raise ProtocolError(f"unknown HUD mode {mode!r}; use one of {', '.join(HUD_MODES)}")
    return {
        "type": "hud",
        "mode": mode,
        "you": clip(you, HUD_LIMITS["you"]),
        "reply": clip(reply, HUD_LIMITS["reply"]),
        "status": clip(status, HUD_LIMITS["status"]),
    }


def ping() -> dict[str, Any]:
    return {"type": "ping"}
