import type { Finding, SportAnalysis } from "./types.ts";

export const TERMINAL_STAGES = new Set(["ready", "error", "no_swing", "rejected"]);
export const FAILED_STAGES = new Set(["error", "no_swing", "rejected"]);

/**
 * The one fix to show first. Findings arrive sorted by severity; entries with
 * `strength: true` describe something done well and are skipped unless nothing else is left.
 */
export function mainFix(analysis: Pick<SportAnalysis, "findings"> | null | undefined): Finding | null {
  const findings = (analysis?.findings ?? []).filter((f): f is Finding => !!f && typeof f === "object");
  return findings.find((f) => !f.strength) ?? findings[0] ?? null;
}

export interface FixSummary {
  id: string;
  headline: string;
  metric: string;
  metricCaption: string;
  cue: string;
  drill: string;
  why: string;
  check: string;
}

/** The plain-language parts of a finding that fit a small screen or a voice prompt. */
export function fixSummary(finding: Finding): FixSummary {
  return {
    id: String(finding.id ?? ""),
    headline: String(finding.headline ?? finding.title ?? ""),
    metric: String(finding.metric?.value ?? ""),
    metricCaption: String(finding.metric?.caption ?? ""),
    cue: String(finding.cue ?? ""),
    drill: String(finding.drill ?? ""),
    why: String(finding.why ?? ""),
    check: String(finding.check ?? ""),
  };
}
