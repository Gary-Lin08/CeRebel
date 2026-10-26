"""Cerebel AI glasses integration: the 9080 link, HUD cards, recordings and the analysis bridge."""

from cerebel.glasses.bridge import GlassesBridge
from cerebel.glasses.clips import FolderWatcher, prepare_clip
from cerebel.glasses.hud import (
    CommandHudSink,
    ConsoleHudSink,
    HudSink,
    LinkHudSink,
    error_card,
    fix_card,
    progress_card,
)
from cerebel.glasses.link import GlassesLink
from cerebel.glasses.mock import MockGlasses
from cerebel.glasses.protocol import DEFAULT_PORT, FrameDecoder, encode_frame, hud

__all__ = [
    "DEFAULT_PORT",
    "CommandHudSink",
    "ConsoleHudSink",
    "FolderWatcher",
    "FrameDecoder",
    "GlassesBridge",
    "GlassesLink",
    "HudSink",
    "LinkHudSink",
    "MockGlasses",
    "encode_frame",
    "error_card",
    "fix_card",
    "hud",
    "prepare_clip",
    "progress_card",
]
