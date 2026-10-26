"""What to put on the waveguide, and where to send it.

The display is a 640x480 single-colour waveguide in front of one eye. A card carries at most
three short fields (status, reply, you) and should be readable in one glance: one fix, one cue.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any, Protocol, TextIO

from cerebel.analysis import fix_summary
from cerebel.glasses.protocol import hud

LOG = logging.getLogger("cerebel.glasses")

# About this many characters fit one waveguide line in the existing live-coach renderer.
LINE_CHARS = 24

_STAGE_TEXT = {
    "zh": {
        "uploading": "上传中",
        "extracting": "准备视频",
        "queued": "排队中",
        "gemx": "重建身体",
        "ik": "计算关节角度",
        "processing": "分析中",
        "ready": "分析完成",
    },
    "en": {
        "uploading": "Uploading",
        "extracting": "Preparing video",
        "queued": "In line",
        "gemx": "Rebuilding body",
        "ik": "Measuring joints",
        "processing": "Analyzing",
        "ready": "Analysis ready",
    },
}

_TEXT = {
    "zh": {
        "received": "收到录像",
        "fix": "要改",
        "no_fix": "这次没有明显要改的地方",
        "failed": "分析失败",
        "no_swing": "没识别到完整挥杆，请从侧面重拍一次",
        "consent": "请先在手机上同意视频处理条款",
        "quota": "本月免费分析次数已用完",
        "offline": "分析服务暂时离线，请稍后再试",
        "rejected": "视频不合格",
        "ahead": "前面 {n} 个",
    },
    "en": {
        "received": "Clip received",
        "fix": "Fix",
        "no_fix": "Nothing big to fix this time",
        "failed": "Analysis failed",
        "no_swing": "No full swing found. Film again from the side",
        "consent": "Agree to video processing in the app first",
        "quota": "This month's free analyses are used up",
        "offline": "Analysis is offline. Try again shortly",
        "rejected": "Video not usable",
        "ahead": "{n} ahead",
    },
}


def _t(lang: str) -> dict[str, str]:
    return _TEXT.get(lang, _TEXT["en"])


def status_card(text: str, *, reply: str = "") -> dict[str, Any]:
    return hud(mode="info", status=text, reply=reply)


def received_card(lang: str = "zh") -> dict[str, Any]:
    return status_card(_t(lang)["received"])


def progress_card(progress: dict[str, Any], lang: str = "zh") -> dict[str, Any]:
    """`Rebuilding body 45%`, plus the queue position while the clip waits for the GPU."""
    stage = str(progress.get("stage") or "processing")
    label = _STAGE_TEXT.get(lang, _STAGE_TEXT["en"]).get(stage, _STAGE_TEXT["en"]["processing"])
    percent = progress.get("percent")
    status = f"{label} {int(percent)}%" if isinstance(percent, (int, float)) else label
    reply = ""
    ahead = progress.get("jobs_ahead")
    if isinstance(ahead, int) and ahead > 0:
        reply = _t(lang)["ahead"].format(n=ahead)
        if progress.get("eta_minutes"):
            reply += f" · ~{progress['eta_minutes']} min"
    return hud(mode="info", status=status, reply=reply)


def fix_card(finding: dict[str, Any] | None, lang: str = "zh") -> dict[str, Any]:
    """The main fix: the measured change in the status line, headline and feel cue below.

    Headline and cue come from the server as written (English today).
    """
    if not finding:
        return hud(mode="assist", status=_STAGE_TEXT.get(lang, _STAGE_TEXT["en"])["ready"], reply=_t(lang)["no_fix"])
    parts = fix_summary(finding)
    status = _t(lang)["fix"] + (f" {parts['metric']}" if parts["metric"] else "")
    reply = parts["headline"]
    if parts["cue"]:
        reply = f"{reply}. {parts['cue']}" if reply else parts["cue"]
    return hud(mode="assist", status=status, reply=reply)


def error_card(kind: str, lang: str = "zh", *, detail: str = "") -> dict[str, Any]:
    """kind: failed | no_swing | consent | quota | offline | rejected."""
    words = _t(lang)
    status = words["rejected"] if kind == "rejected" else words["failed"]
    reply = detail if kind in ("failed", "rejected") and detail else words.get(kind, detail)
    return hud(mode="end", status=status, reply=reply)


def card_text(card: dict[str, Any]) -> str:
    """Flatten a card to plain text for displays driven by a command (one field per line)."""
    return "\n".join(str(card.get(k) or "") for k in ("status", "reply", "you") if card.get(k))


class HudSink(Protocol):
    def show(self, card: dict[str, Any]) -> None: ...


class ConsoleHudSink:
    """Print cards; for dry runs and for development without glasses."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout

    def show(self, card: dict[str, Any]) -> None:
        print(f"[hud:{card.get('mode')}] {card_text(card)}".replace("\n", " | "), file=self.stream, flush=True)


class LinkHudSink:
    """Send cards over a connected GlassesLink (the 9080 protocol)."""

    def __init__(self, link: Any) -> None:
        self.link = link

    def show(self, card: dict[str, Any]) -> None:
        self.link.send(card)


class CommandHudSink:
    """Run a local command per card, e.g. the ADB display scripts in cerebel-ar-glasses/adb.

    `argv` is a list; the item "{text}" is replaced by the card text. No shell is involved, so
    the text cannot inject commands.
    Example: ["powershell", "-NoProfile", "-File", r"adb\\show_text.ps1", "-Text", "{text}"]
    """

    def __init__(self, argv: list[str], *, timeout: float = 20.0) -> None:
        if not argv:
            raise ValueError("argv is empty")
        self.argv = list(argv)
        self.timeout = timeout

    def show(self, card: dict[str, Any]) -> None:
        text = card_text(card)
        args = [text if a == "{text}" else a for a in self.argv]
        try:
            subprocess.run(args, check=False, timeout=self.timeout, capture_output=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("HUD command failed: %s", exc)
