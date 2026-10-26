"""Helpers for reading analysis payloads: which fix to show first, and a compact summary of it."""

from __future__ import annotations

from typing import Any

# Final progress stages. "ready" is the only one with a report.
TERMINAL_STAGES = frozenset({"ready", "error", "no_swing", "rejected"})
FAILED_STAGES = frozenset({"error", "no_swing", "rejected"})


def main_fix(analysis: dict[str, Any]) -> dict[str, Any] | None:
    """The one fix to show first.

    The server already sorts `findings` by severity, biggest first. Entries with
    `strength: true` describe something done well, so they are skipped unless nothing else is left.
    """
    findings = [f for f in (analysis.get("findings") or []) if isinstance(f, dict)]
    for finding in findings:
        if not finding.get("strength"):
            return finding
    return findings[0] if findings else None


def fix_summary(finding: dict[str, Any]) -> dict[str, str]:
    """The plain-language parts of a finding that fit a small screen or a voice prompt."""
    metric = finding.get("metric") if isinstance(finding.get("metric"), dict) else {}
    return {
        "id": str(finding.get("id") or ""),
        "headline": str(finding.get("headline") or finding.get("title") or ""),
        "metric": str(metric.get("value") or ""),
        "metric_caption": str(metric.get("caption") or ""),
        "cue": str(finding.get("cue") or ""),
        "drill": str(finding.get("drill") or ""),
        "why": str(finding.get("why") or ""),
        "check": str(finding.get("check") or ""),
    }
