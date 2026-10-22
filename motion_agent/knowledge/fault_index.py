"""Load Sport Pack finding cards and map detector finding ids → KB fault_*."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

CORPUS_ROOT = Path(__file__).resolve().parent / "corpus"

# Detector / legacy analysis ids → canonical KB fault_* (golf + baseball live packs).
DETECTOR_TO_FAULT: dict[str, dict[str, str]] = {
    "golf": {
        "trail-knee": "fault_setup_locked_legs",
        "lead-arm": "fault_lead_arm_collapse_top",
        "separation": "fault_incomplete_shoulder_turn",
        "early-extension": "fault_early_extension",
        "pelvis-drift": "fault_early_extension",
        "cast": "fault_cast_from_top",
        "over-the-top": "fault_over_the_top_transition",
        "hanging-back": "fault_hanging_back",
        "sway": "fault_sway_not_turn",
        "slide": "fault_slide_excessive",
        "chicken-wing": "fault_chicken_wing_stop",
        "tempo": "fault_tempo_hitch_at_top",
    },
    "baseball": {
        "back-leg": "fault_back_leg_soft",
        "front-knee": "fault_front_knee_collapse",
        "glove": "fault_glove_flyout",
        "arm-path": "fault_arm_path_wide",
        "separation": "fault_separation_low",
        "stride": "fault_stride_short",
        "head": "fault_head_pull",
        "mer": "fault_mer_insufficient",
    },
}

_CARD_FIELDS = (
    "finding_id",
    "headline_en",
    "headline",
    "phases",
    "body_signal",
    "why_it_matters",
    "feel_cue",
    "drill",
    "severity_default",
    "sources",
    "youth_note",
    "legacy_id",
    "confidence_note",
)


def _parse_cards(text: str) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for block in re.split(r"(?m)^---\s*$", text):
        if not re.search(r"(?m)^(?:finding_)?id:\s*", block):
            continue
        fields: dict[str, str] = {}
        for key in _CARD_FIELDS:
            m = re.search(rf"(?ms)^{key}:\s*(.+?)(?=\n[a-z_]+:|\Z)", block)
            if m:
                fields[key] = m.group(1).strip().strip('"')
        fid = fields.get("finding_id") or fields.get("id")
        if not fid:
            continue
        if not fid.startswith("fault_"):
            # tolerate legacy bare id field
            alt = fields.get("finding_id")
            if alt and alt.startswith("fault_"):
                fid = alt
            else:
                continue
        fields["finding_id"] = fid
        cards.append(fields)
    return cards


@lru_cache(maxsize=32)
def load_sport_faults(sport: str) -> dict[str, dict[str, str]]:
    """Return finding_id → card dict for one sport pack."""
    key = (sport or "").strip().lower()
    if key == "general":
        key = "human"
    path = CORPUS_ROOT / key / "Findings" / "001_common_faults.txt"
    if not path.is_file():
        return {}
    out: dict[str, dict[str, str]] = {}
    for card in _parse_cards(path.read_text(encoding="utf-8", errors="ignore")):
        fid = card["finding_id"]
        out[fid] = card
        legacy = card.get("legacy_id")
        if legacy:
            out[legacy] = card
    return out


def resolve_fault_id(sport: str, detector_id: str | None) -> str | None:
    """Map a detector finding id to a KB fault_* id when known."""
    if not detector_id:
        return None
    sport_key = (sport or "").strip().lower()
    det = str(detector_id).strip()
    mapped = DETECTOR_TO_FAULT.get(sport_key, {}).get(det)
    if mapped:
        return mapped
    # Already a fault_* id
    if det.startswith("fault_"):
        return det
    # kebab → fault_snake
    snake = "fault_" + det.replace("-", "_")
    faults = load_sport_faults(sport_key)
    if snake in faults:
        return snake
    return None


def get_fault_card(sport: str, fault_id: str | None) -> dict[str, str] | None:
    if not fault_id:
        return None
    return load_sport_faults(sport).get(fault_id)


def enrich_finding(sport: str, note: dict[str, Any]) -> dict[str, Any]:
    """Attach KB fault_* id and fill missing cue/drill/why from the corpus card."""
    out = dict(note)
    fault_id = resolve_fault_id(sport, note.get("id") or note.get("finding_id"))
    card = get_fault_card(sport, fault_id) if fault_id else None
    if fault_id:
        out["fault_id"] = fault_id
        out["kb_finding_id"] = fault_id
    if not card:
        return out
    if not out.get("headline") and card.get("headline_en"):
        out["headline"] = card["headline_en"]
    if not out.get("cue") and card.get("feel_cue"):
        out["cue"] = card["feel_cue"].strip('"')
    if not out.get("drill") and card.get("drill"):
        out["drill"] = card["drill"]
    if not out.get("why") and card.get("why_it_matters"):
        out["why"] = card["why_it_matters"]
    if card.get("youth_note"):
        out["youth_note"] = card["youth_note"]
    if card.get("sources"):
        out["kb_sources"] = card["sources"]
    if card.get("phases"):
        out["kb_phases"] = card["phases"]
    out["kb_authority"] = _authority_tag(card.get("sources") or "")
    return out


def enrich_findings(sport: str, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [enrich_finding(sport, n) for n in findings]


def faults_for_phase(sport: str, phase_token: str) -> list[str]:
    """List fault_* ids whose phases field mentions phase_token."""
    token = (phase_token or "").strip().lower()
    if not token:
        return []
    hits: list[str] = []
    for fid, card in load_sport_faults(sport).items():
        if not fid.startswith("fault_"):
            continue
        phases = (card.get("phases") or "").lower()
        if token in phases or token.replace("_", "") in phases.replace("_", ""):
            hits.append(fid)
    return sorted(set(hits))


def _authority_tag(sources: str) -> str:
    m = re.search(r"\b(A\+|A|B\+|B|C)\b", sources)
    return m.group(1) if m else ""


def known_sports_with_faults() -> list[str]:
    skip = {"_templates", "fis"}
    out = []
    for p in sorted(CORPUS_ROOT.iterdir()):
        if p.is_dir() and p.name not in skip and (p / "Findings" / "001_common_faults.txt").is_file():
            out.append(p.name)
    return out
