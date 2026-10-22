"""Baseball Sport Pack — detector hook (not live until it can refuse golf honestly)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from motion_agent.sports.schema import DetectResult

BASEBALL_QUESTIONS = [
    "What's the biggest thing I should fix in this pitch?",
    "How does my front knee change from landing to release?",
    "What does my hip–shoulder separation look like?",
    "Does my glove side pull off too early?",
]

# Stay off until labeled pitch clips pass the golf false-positive suite.
DETECTOR_ENABLED = False
# Golf clips can score ~0.8 on the raw pitch heuristic — keep the bar high.
CONFIDENCE_MIN = 0.9


class BaseballPack:
    id = "baseball"
    label = "Baseball / softball"
    status = "coming_soon"  # flip to "live" when DETECTOR_ENABLED + calibrated
    blurb = "Swing + throwing packs — unlock when we ship a detector."
    prompt_mode = "baseball"
    example_questions = list(BASEBALL_QUESTIONS)
    product_title = "Your pitch"
    product_subtitle = "One main fix from landing to release. Scrub the phases. Ask only if you want more."

    def detect(
        self,
        session_dir: Path,
        *,
        video_id: str | None = None,
        data_dir: Path | None = None,
    ) -> DetectResult:
        # Never match a golf swing as a pitch.
        try:
            from motion_agent.sports.packs.golf import GolfPack

            golf = GolfPack().detect(session_dir, video_id=video_id, data_dir=data_dir)
            if golf.matched:
                return DetectResult(
                    matched=False,
                    confidence=0.0,
                    detail={"blocked_by": "golf", "golf": golf.to_dict()},
                    message="This capture looks like a golf swing, not a pitch.",
                )
        except Exception:
            pass

        from motion_agent.sports.packs.pitch_score import pitch_likeness

        scored = pitch_likeness(session_dir, video_id=video_id, data_dir=data_dir)
        score = float(scored.get("score") or 0.0)
        detail = {"pitch_score": scored, "threshold": CONFIDENCE_MIN, "enabled": DETECTOR_ENABLED}

        if not DETECTOR_ENABLED:
            return DetectResult(
                matched=False,
                confidence=score,
                detail=detail,
                message="Baseball pack is coming soon — pitch detector not enabled yet.",
            )

        if not scored.get("ok") or score < CONFIDENCE_MIN:
            return DetectResult(
                matched=False,
                confidence=score,
                detail=detail,
                message=scored.get("message") or "No confident pitch detected in this capture.",
            )
        return DetectResult(matched=True, confidence=score, detail=detail)

    def build_briefs(
        self,
        *,
        video_id: str | None = None,
        data_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        if not DETECTOR_ENABLED:
            return []
        from motion_agent.app.coach_briefs import build_coach_briefs

        payload = build_coach_briefs(video_id=video_id, data_dir=data_dir, sport="baseball")
        if payload.get("error"):
            return []
        return list(payload.get("briefs") or [])
