"""Product session surface — Sport Packs, modes, and ask chips.

Playground is the default UI. The slim consumer shell is ?consumer=1.
The session API always advertises what this capture can honestly support.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from motion_agent.sports.registry import catalog, resolve_pack_session

# Re-exports for older imports / tests.
CONSUMER_GOLF_QUESTIONS = [
    "What's the biggest thing I should fix in this swing?",
    "Walk me through my downswing.",
    "Is my lead arm collapsing before impact?",
    "Where does my pelvis drift after impact?",
]

CONSUMER_GENERAL_QUESTIONS = [
    "What is the person doing in this clip?",
    "What changes in the second half of the motion?",
    "What should I watch for in the posture?",
]


def session_looks_like_golf(
    session_dir: Path,
    *,
    video_id: str | None = None,
    data_dir: Path | None = None,
) -> tuple[bool, float, dict[str, Any] | None]:
    """Return (is_golf, confidence, golf_phases_payload) — thin wrapper for tests."""
    from motion_agent.sports.packs.golf import GolfPack

    pack = GolfPack()
    result = pack.detect(session_dir, video_id=video_id, data_dir=data_dir)
    payload = None
    if result.matched or result.detail:
        try:
            from motion_agent.llm.agent import get_golf_phase_cards

            sid = video_id or session_dir.name
            payload = get_golf_phase_cards(
                video_id=sid, data_dir=data_dir or session_dir.parent.parent
            )
        except Exception:
            payload = None
    return bool(result.matched), float(result.confidence), payload


def build_product_session_fields(
    session_dir: Path,
    *,
    data_dir: Path | None = None,
    preferred_sport: str | None = None,
) -> dict[str, Any]:
    """Fields merged into GET /api/session for a consumer-ready client."""
    pack_session = resolve_pack_session(
        session_dir,
        video_id=session_dir.name,
        data_dir=data_dir,
        preferred=preferred_sport,
    )
    fields = pack_session.as_session_fields()
    fields["sports_catalog"] = catalog()
    return fields


def sports_catalog_payload() -> dict[str, Any]:
    """GET /api/sports — honest live vs coming-soon catalog."""
    rows = catalog()
    live = [r for r in rows if r.get("status") == "live" and r.get("id") != "general"]
    return {
        "ok": True,
        "sports": rows,
        "live_count": len(live),
        "note": "Only live packs run analysis. Coming-soon rows never fake a mode.",
    }


def resolve_prompt_mode_for_session(
    *,
    video_id: str | None,
    data_dir: Path | None,
    requested: str | None,
    allow_dev_modes: bool = False,
) -> tuple[str, dict[str, Any]]:
    """
    Clamp chat prompt_mode to what this capture can honestly support.

    Dev UIs may still request snowboarding/baseball presets; consumer traffic
    is forced onto the resolved Sport Pack mode.
    """
    from motion_agent.sports.registry import get_pack, resolve_pack_session

    meta: dict[str, Any] = {"requested": requested, "clamped": False}
    if not video_id:
        mode = (requested or "general").strip().lower()
        meta["sport"] = None
        return mode, meta

    session_dir = (data_dir or Path(".")) / "sessions" / str(video_id)
    if not session_dir.is_dir():
        mode = (requested or "general").strip().lower()
        meta["sport"] = None
        return mode, meta

    pack_session = resolve_pack_session(
        session_dir, video_id=str(video_id), data_dir=data_dir, preferred=requested
    )
    honest = pack_session.default_prompt_mode
    meta["sport"] = pack_session.sport
    meta["available_modes"] = list(pack_session.available_modes)

    req = (requested or "").strip().lower() or None
    if allow_dev_modes and req:
        # Dev may explore presets, but refuse dressing golf as baseball.
        if req == "baseball" and pack_session.sport == "golf":
            meta["clamped"] = True
            meta["reason"] = "SPORT_MISMATCH"
            return honest, meta
        pack = get_pack(req)
        if pack is not None and getattr(pack, "status", "live") != "live" and pack.id != pack_session.sport:
            meta["clamped"] = True
            meta["reason"] = "pack_not_live"
            return honest, meta
        return req, meta

    if req and req in pack_session.available_modes:
        return req, meta
    if req and req != honest:
        meta["clamped"] = True
        meta["reason"] = "not_available_for_capture"
        return honest, meta
    return honest, meta
