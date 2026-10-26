/**
 * Shapes of the main API answers. Fields the server may add later are allowed by the index
 * signatures; only the fields the SDK and its examples rely on are spelled out.
 * The full contract is sdk/openapi.yaml.
 */

export type Stage =
  | "uploading"
  | "extracting"
  | "queued"
  | "gemx"
  | "ik"
  | "processing"
  | "ready"
  | "no_swing"
  | "error"
  | "rejected";

export interface Progress {
  session_id: string;
  stage: Stage | string;
  percent?: number | null;
  message?: string;
  /** Set once sport detection has run (final `ready` and `no_swing`). */
  sport?: string;
  sport_confidence?: number;
  consumer_ready?: boolean;
  jobs_ahead?: number;
  running?: boolean;
  eta_minutes?: number;
  queue_message?: string;
  failed_at?: string;
  warnings?: string[];
  [key: string]: unknown;
}

export interface User {
  user_id: string;
  email: string;
}

export interface AuthResult {
  ok: true;
  token: string;
  user: User;
}

export interface Quota {
  plan: "free" | "pro";
  monthly_limit: number;
  used_this_month: number;
  remaining: number;
  resets: string;
}

export interface VideoConsent {
  version: string;
  required: boolean;
  accepted_version: string | null;
  accepted_at: string | null;
  text: string;
  raw_video_retention_days?: number;
  inactive_delete_days?: number;
  [key: string]: unknown;
}

export interface Capture {
  session_id: string;
  filename: string;
  status: string;
  created_at: string;
  sport?: string;
  sport_label?: string;
  progress?: Progress | null;
  [key: string]: unknown;
}

export interface Library {
  ok: true;
  user: User;
  captures: Capture[];
  compare: { default: { before: string; after: string } | null; any_pair: boolean };
  quota: Quota;
  subscription: Record<string, unknown>;
  billing: Record<string, unknown>;
  video_consent: VideoConsent;
}

export interface Me extends Partial<Omit<Library, "user">> {
  ok: true;
  user: User | null;
}

export interface UploadStarted {
  ok: true;
  session_id: string;
  filename?: string;
  bytes?: number;
  stage: string;
  warnings: string[];
}

export interface Phase {
  id: string;
  label: string;
  start_time: number;
  end_time: number;
  key_time?: number;
  [key: string]: unknown;
}

export interface Finding {
  id: string;
  title?: string;
  headline?: string;
  plain?: string;
  detail?: string;
  metric?: { value?: string; caption?: string };
  why?: string;
  cue?: string;
  drill?: string;
  check?: string;
  strength?: boolean;
  severity?: number;
  start_time?: number;
  end_time?: number;
  fault_id?: string;
  [key: string]: unknown;
}

export interface SportAnalysis {
  error: boolean;
  sport: string;
  session_id?: string;
  phases: Phase[];
  series: Array<{ id: string; label: string; unit: string; times: number[]; values: number[] }>;
  landmarks: Array<Record<string, unknown>>;
  /** Biggest fix first. */
  findings: Finding[];
  [key: string]: unknown;
}

export type CoachEventType = "status" | "thinking" | "assistant" | "tool" | "final" | "error" | "done";

export interface CoachEvent {
  type: CoachEventType | string;
  interim?: boolean;
  message?: string;
  name?: string | null;
  arguments?: Record<string, unknown> | null;
  content?: string | null;
  /** On `done`: the full answer. */
  answer?: string;
  session_id?: string;
  tool_calls_used?: number;
  error?: string | null;
  [key: string]: unknown;
}

export interface ChatAnswer {
  answer: string;
  session_id: string;
  tool_calls_used: number;
  trace: Array<Record<string, unknown>>;
  error: string | null;
}

export interface AnalyzeResult {
  session_id: string;
  progress: Progress;
  analysis: SportAnalysis;
  main_fix: Finding | null;
}
