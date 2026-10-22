"""Sport Packs — detectors, catalog, and session wiring for multi-sport CeRebel."""

from motion_agent.sports.registry import (
    catalog,
    detect_sport,
    get_pack,
    pack_session_fields,
    resolve_pack_session,
)
from motion_agent.sports.schema import DetectResult, PackSession, SportCatalogEntry, SportPack

__all__ = [
    "DetectResult",
    "PackSession",
    "SportCatalogEntry",
    "SportPack",
    "catalog",
    "detect_sport",
    "get_pack",
    "pack_session_fields",
    "resolve_pack_session",
]
