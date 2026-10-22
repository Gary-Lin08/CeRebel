#!/usr/bin/env python3
"""Report Sport Pack KB completeness vs SPORT_PACK_KB_SPEC."""
from __future__ import annotations

import sys
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1]
REQUIRED = [
    "_meta/pack_readme.txt",
    "_meta/SOURCE_MANIFEST.txt",
    "_meta/brain_plan.txt",
    "_meta/research_notes.txt",
    "_meta/review_report.txt",
    "_meta/build_report.txt",
    "Docs/001_ARCHITECTURE.txt",
    "Docs/002_DISCLAIMER.txt",
    "Schema/001_ontology.txt",
    "Taxonomy/001_concepts.txt",
    "Taxonomy/002_relations.txt",
    "Phases/001_phase_model.txt",
    "Quality/001_quality_definition.txt",
    "Quality/002_checkpoints.txt",
    "Findings/001_common_faults.txt",
    "Coaching/001_cues.txt",
    "Coaching/002_drills.txt",
    "Biomechanics/001_overview.txt",
    "Training/001_team_practice.txt",
]
# Min bytes for "substantial" RAG content (meta can be smaller)
MIN_BYTES = {
    "_meta/pack_readme.txt": 80,
    "_meta/SOURCE_MANIFEST.txt": 200,
    "_meta/brain_plan.txt": 400,
    "_meta/research_notes.txt": 400,
    "_meta/review_report.txt": 200,
    "_meta/build_report.txt": 80,
}
DEFAULT_MIN = 800


def sports() -> list[str]:
    skip = {"_templates", "fis"}
    out = []
    for p in sorted(CORPUS.iterdir()):
        if p.is_dir() and p.name not in skip and not p.name.startswith("."):
            out.append(p.name)
    return out


def check(sport: str) -> tuple[list[str], list[str], int]:
    root = CORPUS / sport
    missing, thin = [], []
    total = 0
    for rel in REQUIRED:
        path = root / rel
        if not path.is_file():
            missing.append(rel)
            continue
        size = path.stat().st_size
        total += size
        need = MIN_BYTES.get(rel, DEFAULT_MIN)
        if size < need:
            thin.append(f"{rel} ({size}B < {need}B)")
    return missing, thin, total


def main() -> int:
    print(f"Corpus: {CORPUS}\n")
    incomplete = 0
    for sport in sports():
        missing, thin, total = check(sport)
        status = "OK"
        if missing:
            status = "MISSING"
            incomplete += 1
        elif thin:
            status = "THIN"
            incomplete += 1
        print(f"[{status:7}] {sport:14} {total:7}B  miss={len(missing)} thin={len(thin)}")
        for m in missing[:6]:
            print(f"           - missing {m}")
        if len(missing) > 6:
            print(f"           - ... +{len(missing) - 6} more")
        for t in thin[:4]:
            print(f"           - thin {t}")
    return 1 if incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
