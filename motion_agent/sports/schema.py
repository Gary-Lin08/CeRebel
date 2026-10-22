"""Sport Pack contract — one honest detector + coaching surface per sport."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class SportCatalogEntry:
    """Public catalog row (live or coming-soon)."""

    id: str
    label: str
    status: str  # "live" | "coming_soon" | "beta"
    blurb: str
    example_questions: tuple[str, ...] = ()
    icon: str = ""


@dataclass
class DetectResult:
    matched: bool
    confidence: float
    detail: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": bool(self.matched),
            "confidence": round(float(self.confidence), 3),
            "detail": dict(self.detail),
            "message": self.message or "",
        }


@dataclass
class PackSession:
    """Resolved pack for one capture — what the consumer UI should show."""

    sport: str
    label: str
    confidence: float
    product_title: str
    product_subtitle: str
    example_questions: list[str]
    default_prompt_mode: str
    available_modes: list[str]
    coach_briefs: list[dict[str, Any]] = field(default_factory=list)
    consumer_ready: bool = True
    pack_status: str = "live"
    detector_detail: dict[str, Any] = field(default_factory=dict)
    detect_message: str = ""

    def as_session_fields(self) -> dict[str, Any]:
        return {
            "sport": self.sport,
            "sport_label": self.label,
            "sport_confidence": round(float(self.confidence), 3),
            "default_prompt_mode": self.default_prompt_mode,
            "available_modes": list(self.available_modes),
            "product_title": self.product_title,
            "product_subtitle": self.product_subtitle,
            "example_questions": list(self.example_questions),
            "coach_briefs": list(self.coach_briefs),
            "consumer_ready": bool(self.consumer_ready),
            "pack_status": self.pack_status,
            "detector": self.detector_detail,
            "detect_message": self.detect_message or "",
        }


class SportPack(Protocol):
    id: str
    label: str
    status: str
    blurb: str
    prompt_mode: str
    example_questions: list[str]
    product_title: str
    product_subtitle: str

    def detect(
        self,
        session_dir: Any,
        *,
        video_id: str | None = None,
        data_dir: Any = None,
    ) -> DetectResult: ...

    def build_briefs(
        self,
        *,
        video_id: str | None = None,
        data_dir: Any = None,
    ) -> list[dict[str, Any]]: ...
