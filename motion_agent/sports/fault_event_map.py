"""Phase-event → fault_* wiring for Sport Packs.

Built from corpus Phases `fault_links` when present, plus detector id maps
from `motion_agent.knowledge.fault_index.DETECTOR_TO_FAULT`.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from motion_agent.knowledge.fault_index import (
    DETECTOR_TO_FAULT,
    faults_for_phase,
    load_sport_faults,
    resolve_fault_id,
)

CORPUS = Path(__file__).resolve().parents[1] / "knowledge" / "corpus"


def _parse_phase_fault_links(text: str) -> dict[str, list[str]]:
    """Extract phase_id → [fault_*] from phase model files."""
    mapping: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        # ### PHASE_ID or ## PHASE_ID
        m = re.match(r"^#{2,3}\s+`?([A-Za-z][\w\-]*)`?\s*$", line.strip())
        if m:
            current = m.group(1)
            mapping.setdefault(current, [])
            continue
        if current is None:
            continue
        if re.match(r"(?i)^\s*fault_links\s*:", line):
            rest = line.split(":", 1)[1].strip()
            ids = re.findall(r"fault_[\w]+", rest)
            if not ids:
                # maybe next lines are a list
                continue
            mapping[current] = sorted(set(mapping[current] + ids))
            continue
        if re.match(r"(?i)^\s*-\s*fault_", line.strip()):
            fid = re.findall(r"fault_[\w]+", line)
            mapping[current] = sorted(set(mapping[current] + fid))
    return {k: v for k, v in mapping.items() if v}


@lru_cache(maxsize=32)
def phase_fault_map(sport: str) -> dict[str, list[str]]:
    key = (sport or "").strip().lower()
    path = CORPUS / key / "Phases" / "001_phase_model.txt"
    if not path.is_file():
        return {}
    return _parse_phase_fault_links(path.read_text(encoding="utf-8", errors="ignore"))


def detector_fault_map(sport: str) -> dict[str, str]:
    return dict(DETECTOR_TO_FAULT.get((sport or "").strip().lower(), {}))


def lookup(
    sport: str,
    *,
    detector_id: str | None = None,
    phase_id: str | None = None,
) -> dict[str, Any]:
    """Resolve KB faults for a detector finding and/or phase window."""
    sport_key = (sport or "").strip().lower()
    fault_id = resolve_fault_id(sport_key, detector_id) if detector_id else None
    phase_faults: list[str] = []
    if phase_id:
        phase_faults = list(phase_fault_map(sport_key).get(phase_id) or [])
        if not phase_faults:
            phase_faults = faults_for_phase(sport_key, phase_id)
    return {
        "sport": sport_key,
        "detector_id": detector_id,
        "fault_id": fault_id,
        "phase_id": phase_id,
        "phase_faults": phase_faults,
        "card_loaded": bool(fault_id and fault_id in load_sport_faults(sport_key)),
    }


def catalog_summary() -> list[dict[str, Any]]:
    rows = []
    for sport, mapping in sorted(DETECTOR_TO_FAULT.items()):
        rows.append(
            {
                "sport": sport,
                "detector_links": len(mapping),
                "phase_links": sum(len(v) for v in phase_fault_map(sport).values()),
                "kb_faults": len(
                    [k for k in load_sport_faults(sport) if k.startswith("fault_")]
                ),
            }
        )
    return rows
