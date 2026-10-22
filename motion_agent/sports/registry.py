"""Sport Pack registry — catalog, detectors, and auto-select."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from motion_agent.sports.packs.baseball import BaseballPack
from motion_agent.sports.packs.coming_soon import ComingSoonPack, build_staged_packs
from motion_agent.sports.packs.golf import GolfPack
from motion_agent.sports.packs.human import HumanMotionPack
from motion_agent.sports.schema import DetectResult, PackSession, SportPack

_GOLF = GolfPack()
_HUMAN = HumanMotionPack()
_BASEBALL = BaseballPack()
_STAGED: list[ComingSoonPack] = build_staged_packs()
_STAGED_BY_ID: dict[str, ComingSoonPack] = {p.id: p for p in _STAGED}

LIVE_PACKS: list[SportPack] = [_GOLF, _HUMAN]
# Packs with corpus + catalog row; detectors off (or baseball gated).
STAGED_PACKS: list[SportPack] = [_BASEBALL, *_STAGED]


def get_pack(sport_id: str | None) -> SportPack | None:
    if not sport_id:
        return None
    key = sport_id.strip().lower()
    for pack in LIVE_PACKS:
        if pack.id == key:
            return pack
    for pack in STAGED_PACKS:
        if pack.id == key:
            return pack
        aliases = getattr(pack, "aliases", None) or []
        if key in aliases:
            return pack
    if key in {"golf_swing", "driver", "iron"}:
        return _GOLF
    if key in {"human", "general_motion", "body"}:
        return _HUMAN
    if key in {"pitch", "softball", "throwing"}:
        return _BASEBALL
    return None


def _pack_catalog_row(pack: SportPack, *, locked: bool, featured: bool) -> dict[str, Any]:
    status = "live" if not locked else getattr(pack, "status", "coming_soon") or "coming_soon"
    return {
        "id": pack.id,
        "label": pack.label,
        "status": status,
        "blurb": pack.blurb,
        "example_questions": list(pack.example_questions),
        "product_title": pack.product_title,
        "product_subtitle": pack.product_subtitle,
        "featured": featured,
        "locked": locked,
        "kb_corpus": getattr(pack, "id", None) if pack.id != "general" else "human",
        "prompt_mode": pack.prompt_mode,
    }


def catalog(*, include_coming_soon: bool = True) -> list[dict[str, Any]]:
    """Public sports catalog for the product UI."""
    rows: list[dict[str, Any]] = []
    for pack in LIVE_PACKS:
        rows.append(_pack_catalog_row(pack, locked=False, featured=pack.id != "general"))
    if include_coming_soon:
        for pack in STAGED_PACKS:
            rows.append(_pack_catalog_row(pack, locked=True, featured=False))
    return rows


def detect_sport(
    session_dir: Path,
    *,
    video_id: str | None = None,
    data_dir: Path | None = None,
    preferred: str | None = None,
) -> tuple[SportPack, DetectResult]:
    """
    Auto-select a live pack.

    Prefer an explicit user choice when that pack is live and its detector matches.
    Otherwise run specialized detectors and pick the highest-confidence match;
    fall back to human motion.
    """
    preferred_pack = get_pack(preferred)
    if preferred_pack is not None and preferred_pack.id != "general":
        # Preferred staged packs never auto-activate analysis.
        if preferred_pack in STAGED_PACKS or getattr(preferred_pack, "status", "live") != "live":
            human_result = _HUMAN.detect(session_dir, video_id=video_id, data_dir=data_dir)
            tried = preferred_pack.detect(session_dir, video_id=video_id, data_dir=data_dir)
            return _HUMAN, DetectResult(
                matched=False,
                confidence=0.0,
                detail={
                    "fallback": True,
                    "preferred": preferred_pack.id,
                    "preferred_detect": tried.to_dict(),
                    "pack_locked": True,
                },
                message=tried.message
                or f"{preferred_pack.label} is coming soon — coaching with general motion.",
            )
        result = preferred_pack.detect(session_dir, video_id=video_id, data_dir=data_dir)
        if result.matched:
            return preferred_pack, result

    best: tuple[SportPack, DetectResult] | None = None
    for pack in LIVE_PACKS:
        if pack.id == "general":
            continue
        result = pack.detect(session_dir, video_id=video_id, data_dir=data_dir)
        if not result.matched:
            continue
        if best is None or result.confidence > best[1].confidence:
            best = (pack, result)

    if best is not None:
        return best

    if preferred_pack is not None and preferred_pack.id == "general":
        return preferred_pack, preferred_pack.detect(session_dir, video_id=video_id, data_dir=data_dir)

    human_result = _HUMAN.detect(session_dir, video_id=video_id, data_dir=data_dir)
    if preferred_pack is not None and preferred_pack.id != "general":
        tried = preferred_pack.detect(session_dir, video_id=video_id, data_dir=data_dir)
        human_result = DetectResult(
            matched=False,
            confidence=0.0,
            detail={
                "fallback": True,
                "preferred": preferred_pack.id,
                "preferred_detect": tried.to_dict(),
            },
            message=tried.message or f"Couldn’t confirm {preferred_pack.label.lower()} in this clip.",
        )
    return _HUMAN, human_result


def resolve_pack_session(
    session_dir: Path,
    *,
    video_id: str | None = None,
    data_dir: Path | None = None,
    preferred: str | None = None,
) -> PackSession:
    pack, detect = detect_sport(
        session_dir, video_id=video_id, data_dir=data_dir, preferred=preferred
    )
    briefs: list[dict[str, Any]] = []
    try:
        briefs = list(pack.build_briefs(video_id=video_id or session_dir.name, data_dir=data_dir))
    except Exception:
        briefs = []

    # Consumer: only advertise modes we can honestly run (no fake locked sports).
    modes = [pack.prompt_mode]
    return PackSession(
        sport=pack.id,
        label=pack.label,
        confidence=detect.confidence if detect.matched else 0.0,
        product_title=pack.product_title,
        product_subtitle=pack.product_subtitle,
        example_questions=list(pack.example_questions),
        default_prompt_mode=pack.prompt_mode,
        available_modes=modes,
        coach_briefs=briefs,
        consumer_ready=True,
        pack_status="live" if pack in LIVE_PACKS else getattr(pack, "status", "coming_soon"),
        detector_detail=detect.to_dict(),
        detect_message=detect.message or "",
    )


def pack_session_fields(pack: SportPack, detect: DetectResult) -> dict[str, Any]:
    """Legacy helper — prefer resolve_pack_session for full fields."""
    return {
        "sport": pack.id,
        "sport_label": pack.label,
        "sport_status": getattr(pack, "status", "live"),
        "sport_blurb": pack.blurb,
        "prompt_mode": pack.prompt_mode,
        "default_prompt_mode": pack.prompt_mode,
        "available_modes": [pack.prompt_mode],
        "example_questions": list(pack.example_questions),
        "product_title": pack.product_title,
        "product_subtitle": pack.product_subtitle,
        "detect": detect.to_dict(),
        "catalog": catalog(),
    }
