"""Stub Sport Packs — catalog + prompt_mode + KB wiring; detectors stay off."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from motion_agent.sports.schema import DetectResult


@dataclass
class ComingSoonPack:
    """Honest locked pack: visible in catalog, never auto-selected until live."""

    id: str
    label: str
    blurb: str
    example_questions: list[str]
    product_title: str
    product_subtitle: str
    prompt_mode: str = ""
    status: str = "coming_soon"  # or "beta"
    aliases: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.prompt_mode:
            self.prompt_mode = self.id

    def detect(
        self,
        session_dir: Path,
        *,
        video_id: str | None = None,
        data_dir: Path | None = None,
    ) -> DetectResult:
        return DetectResult(
            matched=False,
            confidence=0.0,
            detail={"enabled": False, "pack": self.id},
            message=f"{self.label} pack is coming soon — detector not enabled yet.",
        )

    def build_briefs(
        self,
        *,
        video_id: str | None = None,
        data_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        return []


def build_staged_packs() -> list[ComingSoonPack]:
    """All non-live specialized packs that have a Sport Pack KB corpus."""
    return [
        ComingSoonPack(
            id="tennis",
            label="Tennis",
            blurb="Serve and groundstroke timing on the body.",
            example_questions=["Is my trophy pose stacked before the racket drop?"],
            product_title="Your stroke",
            product_subtitle="Body timing for serve and groundstrokes — unlock when the detector ships.",
        ),
        ComingSoonPack(
            id="basketball",
            label="Basketball",
            blurb="Shot form and landing balance.",
            example_questions=["Does my guide hand stay quiet through release?"],
            product_title="Your shot",
            product_subtitle="Jump shot and free-throw body form — unlock when the detector ships.",
        ),
        ComingSoonPack(
            id="soccer",
            label="Soccer",
            blurb="Strike and plant-foot mechanics.",
            example_questions=["Is my plant foot closing the hip through the kick?"],
            product_title="Your strike",
            product_subtitle="Plant and swing-leg sequencing — unlock when the detector ships.",
        ),
        ComingSoonPack(
            id="running",
            label="Running",
            blurb="Cadence, posture, and asymmetry over strides.",
            example_questions=["Am I overstriding on the right side?"],
            product_title="Your gait",
            product_subtitle="Stride form cues from body tracking — unlock when the detector ships.",
        ),
        ComingSoonPack(
            id="weightlifting",
            label="Weightlifting",
            blurb="Squat / hinge / press path checks.",
            example_questions=["Does my chest drop before my hips in the squat?"],
            product_title="Your lift",
            product_subtitle="Barbell path and posture — unlock when the detector ships.",
        ),
        ComingSoonPack(
            id="swimming",
            label="Swimming",
            status="beta",
            blurb="Stroke timing needs pool-side capture support.",
            example_questions=["Where does my pull lose elbow height?"],
            product_title="Your stroke",
            product_subtitle="Freestyle timing from deck video — beta until capture + detector are ready.",
        ),
        ComingSoonPack(
            id="volleyball",
            label="Volleyball",
            blurb="Approach and contact sequencing.",
            example_questions=["Am I jumping under the ball or behind it?"],
            product_title="Your approach",
            product_subtitle="Spike approach and contact proxy — unlock when the detector ships.",
        ),
        ComingSoonPack(
            id="boxing",
            label="Boxing / striking",
            blurb="Guard, hip torque, and recovery.",
            example_questions=["Does my rear hand drop when I throw the jab?"],
            product_title="Your shadowboxing",
            product_subtitle="Technique-only jab/cross form — unlock when the detector ships.",
        ),
        ComingSoonPack(
            id="yoga",
            label="Yoga / mobility",
            blurb="Hold quality and joint stacking.",
            example_questions=["Is my lumbar arching in downward dog?"],
            product_title="Your pose",
            product_subtitle="Alignment holds and modifications — unlock when the detector ships.",
        ),
        ComingSoonPack(
            id="snowboarding",
            label="Snowboarding",
            blurb="Stance, turns, and edge control on the body.",
            example_questions=["Am I initiating the turn from the lower body?"],
            product_title="Your ride",
            product_subtitle="Turn and edge body mechanics + FIS rules KB — unlock when the detector ships.",
            aliases=["snowboard", "sb"],
        ),
    ]
