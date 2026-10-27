"""Golf Sport Pack — live detector + briefs over existing swing analysis."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from motion_agent.sports.packs.golf_gate import NO_SWING_MESSAGE, evaluate_golf_gate
from motion_agent.sports.schema import DetectResult

LOG = logging.getLogger("motion_agent.sports.golf")

GOLF_QUESTIONS = [
    "What's the biggest thing I should fix in this swing?",
    "Walk me through my downswing.",
    "Is my lead arm collapsing before impact?",
    "Where does my pelvis drift after impact?",
]

# Minimum swing confidence before we activate the golf pack.
CONFIDENCE_MIN = 0.45


def _best_swing_confidence(phases_payload: dict[str, Any]) -> float:
    if not phases_payload or phases_payload.get("error"):
        return 0.0
    best = 0.0
    for swing in phases_payload.get("swings") or []:
        try:
            best = max(best, float(swing.get("confidence") or 0.0))
        except (TypeError, ValueError):
            continue
    return best


def _golf_gate(session_dir: Path, phases_payload: dict[str, Any]) -> dict[str, Any] | None:
    """Run the "is this really golf" checks; None when the body can't be loaded."""
    from motion_agent.motion.adapter import adapt_session

    try:
        seq = adapt_session(session_dir)
    except Exception:  # noqa: BLE001
        LOG.exception("golf gate could not load %s", session_dir)
        return None
    return evaluate_golf_gate(seq, phases_payload)


class GolfPack:
    id = "golf"
    label = "Golf"
    status = "live"
    blurb = "Swing phases, body fixes, and instant drills."
    prompt_mode = "golf"
    example_questions = list(GOLF_QUESTIONS)
    product_title = "Your swing"
    product_subtitle = "One main fix on your body. Scrub the phases. Ask only if you want more."

    def detect(
        self,
        session_dir: Path,
        *,
        video_id: str | None = None,
        data_dir: Path | None = None,
    ) -> DetectResult:
        from motion_agent.llm.agent import get_golf_phase_cards

        sid = video_id or session_dir.name
        root = data_dir or session_dir.parent.parent
        payload = get_golf_phase_cards(video_id=sid, data_dir=root)
        conf = _best_swing_confidence(payload)
        ok = conf >= CONFIDENCE_MIN and not payload.get("error")
        gate = _golf_gate(root / "sessions" / sid, payload) if ok else None
        detail = {
            "swing_count": len(payload.get("swings") or []) if isinstance(payload, dict) else 0,
            "threshold": CONFIDENCE_MIN,
            "tool": "segment_golf_swings",
        }
        if gate is not None:
            ok = bool(gate["passed"])
            detail["gate"] = gate
            detail["not_golf"] = bool(gate.get("not_golf"))
            detail["looks_like"] = gate.get("looks_like")
        if ok:
            message = ""
        elif gate is not None:
            message = gate["message"]
        else:
            message = NO_SWING_MESSAGE
        return DetectResult(matched=ok, confidence=conf, detail=detail, message=message)

    def build_briefs(
        self,
        *,
        video_id: str | None = None,
        data_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        from motion_agent.app.coach_briefs import build_coach_briefs

        payload = build_coach_briefs(video_id=video_id, data_dir=data_dir, sport="golf")
        if payload.get("error"):
            return []
        return list(payload.get("briefs") or [])
