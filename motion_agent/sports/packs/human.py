"""Honest fallback when no specialized sport pack matches."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from motion_agent.sports.schema import DetectResult

HUMAN_QUESTIONS = [
    "What is the person doing in this clip?",
    "What changes in the second half of the motion?",
    "What should I watch for in the posture?",
]


class HumanMotionPack:
    id = "general"
    label = "Human motion"
    status = "live"
    blurb = "General body coaching when a sport-specific pack isn’t detected (human/ KB)."
    prompt_mode = "general"
    kb_corpus = "human"
    example_questions = list(HUMAN_QUESTIONS)
    product_title = "Your motion"
    product_subtitle = "We’ll coach the body we can measure — sport packs unlock when we recognize the activity."

    def detect(
        self,
        session_dir: Path,
        *,
        video_id: str | None = None,
        data_dir: Path | None = None,
    ) -> DetectResult:
        # Always available as a last-resort pack (never “matches” over a sport pack).
        return DetectResult(matched=False, confidence=0.0, detail={"fallback": True})

    def build_briefs(
        self,
        *,
        video_id: str | None = None,
        data_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "human_posture",
                "question": HUMAN_QUESTIONS[2],
                "answer": (
                    "Watch head–rib–pelvis stack and whether the arms fight the torso. "
                    "When a sport pack unlocks, we’ll name a sharper fix and drill."
                ),
                "finding_id": None,
                "instant": True,
            }
        ]
