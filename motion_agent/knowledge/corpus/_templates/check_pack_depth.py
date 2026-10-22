#!/usr/bin/env python3
"""v2 depth check for Sport Pack KBs (beyond file presence)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1]
SKIP = {"_templates", "fis"}

MIN_FINDINGS = 25
MIN_CUES = 20
MIN_DRILLS = 15
MIN_RELATIONS = 15
REQUIRED_FIELDS = (
    "finding_id",
    "headline_en",
    "phases",
    "body_signal",
    "why_it_matters",
    "feel_cue",
    "drill",
    "severity_default",
    "sources",
)


def sports() -> list[str]:
    return sorted(
        p.name
        for p in CORPUS.iterdir()
        if p.is_dir() and p.name not in SKIP and not p.name.startswith(".")
    )


def finding_ids(text: str) -> list[str]:
    ids = set(re.findall(r"(?mi)^(?:finding_)?id:\s*`?([a-zA-Z][\w\-]*)`?", text))
    ids |= set(re.findall(r"(?m)^FAULT:\s*`?([a-zA-Z][\w\-]*)`?", text))
    ids |= set(re.findall(r"(?m)^##\s+`?([a-zA-Z][\w\-]+)`?", text))
    ids |= set(re.findall(r"(?m)^(fault_[\w\-]+)\s*$", text))
    noise = {
        "index",
        "legacy",
        "severity",
        "finding",
        "additional",
        "end",
        "format",
        "authority",
        "sources",
        "phases",
        "headline",
        "common",
        "overview",
        "notes",
        "youth",
    }
    return sorted(i for i in ids if i.lower() not in noise and len(i) >= 4)


def canonical_cards(text: str) -> list[dict[str, str]]:
    cards = []
    for block in re.split(r"(?m)^---\s*$", text):
        if "finding_id:" not in block and not re.search(r"(?m)^id:\s*fault_", block):
            # also accept id: fault_ without finding_ prefix
            if not re.search(r"(?m)^id:\s*`?fault_", block):
                continue
        fields: dict[str, str] = {}
        for key in REQUIRED_FIELDS + ("id", "headline", "body_measurable_signal", "youth_note"):
            m = re.search(rf"(?ms)^{key}:\s*(.+?)(?=\n[a-z_]+:|\Z)", block)
            if m:
                fields[key] = m.group(1).strip()
        fid = fields.get("finding_id") or fields.get("id")
        if fid:
            fields["finding_id"] = fid
            cards.append(fields)
    return cards


def count_fault_refs(text: str) -> int:
    return len(set(re.findall(r"fault_[\w]+", text)))


def relation_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    lines = [
        ln
        for ln in path.read_text(errors="ignore").splitlines()
        if ln.strip() and not ln.strip().startswith("#") and "predicate" not in ln.lower()
    ]
    # CSV header skip already; also accept "a,rel,b" or prose triples
    return len(lines)


def check(sport: str) -> dict:
    root = CORPUS / sport
    findings_text = (root / "Findings/001_common_faults.txt").read_text(errors="ignore")
    cues_text = (root / "Coaching/001_cues.txt").read_text(errors="ignore")
    drills_text = (root / "Coaching/002_drills.txt").read_text(errors="ignore")
    rel_n = relation_rows(root / "Taxonomy/002_relations.txt")
    ids = finding_ids(findings_text)
    cards = canonical_cards(findings_text)
    non_fault = [i for i in ids if not i.startswith("fault_")]
    full = [
        c
        for c in cards
        if all(
            c.get(k)
            or (k == "headline_en" and c.get("headline"))
            or (k == "body_signal" and c.get("body_measurable_signal"))
            for k in REQUIRED_FIELDS
            if k != "finding_id"
        )
        and c.get("finding_id")
    ]
    # relaxed full: has finding_id + body + cue + drill + sources
    usable = [
        c
        for c in cards
        if c.get("finding_id")
        and (c.get("body_signal") or c.get("body_measurable_signal"))
        and c.get("feel_cue")
        and c.get("drill")
        and c.get("sources")
    ]
    review = (root / "_meta/review_report.txt").read_text(errors="ignore")
    verdict = "missing"
    m = re.search(r"(?m)^VERDICT:\s*(.+)$", review)
    if m:
        verdict = m.group(1).strip()
    elif re.search(r"(?i)APPROVED", review):
        verdict = "legacy-approved"
    gaps = []
    if len(ids) < MIN_FINDINGS:
        gaps.append(f"findings {len(ids)}<{MIN_FINDINGS}")
    if non_fault:
        gaps.append(f"non_fault_ids={len(non_fault)}")
    if count_fault_refs(cues_text) < MIN_CUES and len(ids) >= MIN_FINDINGS:
        # cues may use ## fault_ headings
        cue_heads = len(re.findall(r"(?m)^##\s+", cues_text)) + count_fault_refs(cues_text)
        if cue_heads < MIN_CUES:
            gaps.append(f"cue_refs~{max(cue_heads, count_fault_refs(cues_text))}<{MIN_CUES}")
    drill_refs = count_fault_refs(drills_text) + len(re.findall(r"(?m)^##\s+", drills_text))
    if drill_refs < MIN_DRILLS:
        gaps.append(f"drill_refs~{drill_refs}<{MIN_DRILLS}")
    if rel_n < MIN_RELATIONS:
        gaps.append(f"relations {rel_n}<{MIN_RELATIONS}")
    if "STALE" in verdict.upper() or re.search(
        r"no DISCLAIMER|no SOURCE_MANIFEST", review, re.I
    ):
        if (root / "Docs/002_DISCLAIMER.txt").stat().st_size > 500:
            gaps.append("review_stale")
    return {
        "sport": sport,
        "findings": len(ids),
        "canonical": len(cards),
        "usable": len(usable),
        "non_fault": len(non_fault),
        "cue_refs": count_fault_refs(cues_text),
        "drill_refs": count_fault_refs(drills_text),
        "relations": rel_n,
        "verdict": verdict[:40],
        "gaps": gaps,
    }


def main() -> int:
    print(f"Corpus: {CORPUS}")
    print(f"Targets: findings>={MIN_FINDINGS} cues>={MIN_CUES} drills>={MIN_DRILLS} rel>={MIN_RELATIONS}\n")
    bad = 0
    print(f"{'sport':14} {'N':>3} {'can':>3} {'use':>3} {'cue':>3} {'drl':>3} {'rel':>3} status")
    for s in sports():
        r = check(s)
        ok = not r["gaps"]
        if not ok:
            bad += 1
        flag = "OK  " if ok else "GAP "
        print(
            f"{flag} {r['sport']:12} {r['findings']:3} {r['canonical']:3} {r['usable']:3} "
            f"{r['cue_refs']:3} {r['drill_refs']:3} {r['relations']:3}  {'; '.join(r['gaps']) or 'v2-ready'}"
        )
    print(f"\n{len(sports()) - bad}/{len(sports())} packs meet v2 depth targets")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
