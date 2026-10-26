"""Instant coaching briefs from measured findings — no LLM round-trip."""

from __future__ import annotations

from typing import Any

from motion_agent.app.product_session import CONSUMER_GOLF_QUESTIONS
from motion_agent.llm.agent import get_sport_analysis


def _finding_answer(note: dict[str, Any]) -> str:
    parts: list[str] = []
    headline = note.get("headline") or note.get("title")
    if headline:
        parts.append(str(headline))
    if note.get("plain"):
        parts.append(str(note["plain"]))
    elif note.get("detail"):
        parts.append(str(note["detail"]))
    metric = note.get("metric") or {}
    if metric.get("value") or metric.get("caption"):
        parts.append(
            "Measured: "
            + f"{metric.get('value', '')} — {metric.get('caption', '')}".strip(" —")
        )
    if note.get("why"):
        parts.append(str(note["why"]))
    if note.get("cue"):
        parts.append(f"What to feel: {note['cue']}")
    if note.get("drill"):
        parts.append(f"Try this: {note['drill']}")
    return "\n\n".join(p for p in parts if p)


def _phase_walkthrough(payload: dict[str, Any]) -> str:
    phases = payload.get("phases") or []
    if not phases:
        return "Phase windows are not available for this swing yet."
    lines = ["Here is your swing story by phase:"]
    for phase in phases:
        label = phase.get("label") or phase.get("id") or "Phase"
        t0 = phase.get("start_time")
        t1 = phase.get("end_time")
        try:
            lines.append(f"• {label}: {float(t0):.2f}s → {float(t1):.2f}s")
        except (TypeError, ValueError):
            lines.append(f"• {label}")
    findings = payload.get("findings") or []
    if findings:
        top = findings[0]
        lines.append("")
        lines.append(
            "Biggest issue while you move through these phases: "
            f"{top.get('title') or 'see the main fix'}."
        )
        if top.get("cue"):
            lines.append(f"Hold this feel: {top['cue']}")
    return "\n".join(lines)


# What each consumer chip asks about: detector / KB fault ids, the trace it is read
# from, and title phrases as a last resort. Coaching text mentions other body parts
# ("the arms have to make the speed" on the hip-turn note), so the body text is
# never searched.
_CHIP_LEAD_ARM = {
    "ids": ("lead-arm", "fault_lead_arm_collapse_top"),
    "series": ("lead-elbow",),
    "title": ("lead arm", "lead elbow", "front arm"),
}
_CHIP_PELVIS = {
    "ids": ("early-extension", "fault_early_extension"),
    "series": ("pelvis-drift",),
    "title": ("early extension", "pelvis drift", "hip drift"),
}


def _finding_for_chip(
    findings: list[dict[str, Any]], chip: dict[str, tuple[str, ...]]
) -> dict[str, Any] | None:
    for note in findings:
        ids = {str(note.get(k) or "").lower() for k in ("id", "fault_id")}
        if ids & set(chip["ids"]):
            return note
    for note in findings:
        if str(note.get("series") or "").lower() in chip["series"]:
            return note
    for note in findings:
        title = " ".join(str(note.get(k) or "") for k in ("title", "headline")).lower()
        if any(phrase in title for phrase in chip["title"]):
            return note
    return None


def build_coach_briefs(
    *,
    video_id: str | None = None,
    data_dir=None,
    sport: str = "golf",
) -> dict[str, Any]:
    """Return instant Q→A cards derived from the motion board."""
    payload = get_sport_analysis(sport, video_id=video_id, data_dir=data_dir)
    if payload.get("error"):
        return {
            "error": True,
            "type": payload.get("type") or "ANALYSIS_FAILED",
            "message": payload.get("message") or "No briefs available",
            "briefs": [],
        }
    findings = list(payload.get("findings") or [])
    briefs: list[dict[str, Any]] = []

    def add(qid: str, question: str, answer: str, finding_id: str | None = None) -> None:
        briefs.append(
            {
                "id": qid,
                "question": question,
                "answer": answer,
                "finding_id": finding_id,
                "instant": True,
            }
        )

    try:
        from motion_agent.knowledge.fault_index import enrich_findings

        findings = enrich_findings(sport, findings)
    except Exception:
        pass

    if findings:
        primary = findings[0]
        add(
            "biggest_fix",
            CONSUMER_GOLF_QUESTIONS[0],
            _finding_answer(primary),
            primary.get("fault_id") or primary.get("id"),
        )
    add("downswing_walkthrough", CONSUMER_GOLF_QUESTIONS[1], _phase_walkthrough(payload))

    # Finding briefs for the remaining consumer chips.
    for question, suffix, chip in (
        (CONSUMER_GOLF_QUESTIONS[2], "_arm", _CHIP_LEAD_ARM),
        (CONSUMER_GOLF_QUESTIONS[3], "_pelvis", _CHIP_PELVIS),
    ):
        note = _finding_for_chip(findings, chip)
        if note is not None and len(briefs) < 6:
            add(
                f"finding_{note.get('id')}{suffix}",
                question,
                _finding_answer(note),
                note.get("id"),
            )

    # Dedupe by question text (keep first).
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for brief in briefs:
        q = brief["question"].strip().lower()
        if q in seen:
            continue
        seen.add(q)
        unique.append(brief)

    return {
        "error": False,
        "sport": payload.get("sport") or sport,
        "session_id": video_id,
        "briefs": unique,
        "findings": [
            {
                "id": n.get("id"),
                "title": n.get("title"),
                "answer": _finding_answer(n),
            }
            for n in findings
        ],
    }


def match_brief(question: str, briefs: list[dict[str, Any]]) -> dict[str, Any] | None:
    q = (question or "").strip().lower()
    if not q:
        return None
    for brief in briefs:
        bq = str(brief.get("question") or "").strip().lower()
        if q == bq or q in bq or bq in q:
            return brief
    # Loose keyword match for spoken questions.
    if "pelvis" in q or "hip" in q or "early extension" in q:
        for brief in briefs:
            if "pelvis" in str(brief.get("question") or "").lower() or (
                brief.get("id") or ""
            ).endswith("_pelvis"):
                return brief
    if "lead arm" in q or "arm" in q or "collaps" in q:
        for brief in briefs:
            if "lead arm" in str(brief.get("question") or "").lower() or (
                brief.get("id") or ""
            ).endswith("_arm"):
                return brief
    if "biggest" in q or "fix" in q or "work on" in q:
        for brief in briefs:
            if brief.get("id") == "biggest_fix":
                return brief
    if "downswing" in q or "walk me through" in q or "phases" in q:
        for brief in briefs:
            if brief.get("id") == "downswing_walkthrough":
                return brief
    return None
