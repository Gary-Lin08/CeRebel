/**
 * Cerebel AI glasses link protocol: length-prefixed JSON over TCP (default port 9080).
 * Transport-agnostic: give `encodeFrame` output to any TCP socket (React Native, Node net)
 * and feed received bytes to a FrameDecoder. Same contract as sdk/python/cerebel/glasses.
 *
 * Glasses -> host: hello {fw, battery}, key {action: photo|rec_start|rec_stop}, pcm {seq, sr, ch, data_b64}
 * Host -> glasses: hud {mode: info|assist|fact|end, you, reply, status}, ping
 */

import { fixSummary } from "./analysis.ts";
import type { Finding, Progress } from "./types.ts";

export const DEFAULT_PORT = 9080;
export const MAX_FRAME_BYTES = 1024 * 1024;
export const HUD_MODES = ["info", "assist", "fact", "end"] as const;
export const HUD_LIMITS = { you: 80, reply: 160, status: 40 } as const;

export type HudMode = (typeof HUD_MODES)[number];
export interface GlassesMessage {
  type: string;
  [key: string]: unknown;
}
export interface HudCard extends GlassesMessage {
  type: "hud";
  mode: HudMode;
  you: string;
  reply: string;
  status: string;
}

export class ProtocolError extends Error {}

const encoder = new TextEncoder();
const decoder = new TextDecoder();

export function encodeFrame(message: GlassesMessage): Uint8Array {
  if (!message || typeof message.type !== "string" || !message.type) throw new ProtocolError("a frame is a JSON object with a type");
  const body = encoder.encode(JSON.stringify(message));
  if (body.length > MAX_FRAME_BYTES) throw new ProtocolError(`frame of ${body.length} bytes exceeds ${MAX_FRAME_BYTES}`);
  const out = new Uint8Array(4 + body.length);
  new DataView(out.buffer).setUint32(0, body.length, false);
  out.set(body, 4);
  return out;
}

/** Feed received bytes in any chunking; get back complete messages. */
export class FrameDecoder {
  private buf = new Uint8Array(0);

  feed(chunk: Uint8Array): GlassesMessage[] {
    const merged = new Uint8Array(this.buf.length + chunk.length);
    merged.set(this.buf, 0);
    merged.set(chunk, this.buf.length);
    this.buf = merged;
    const out: GlassesMessage[] = [];
    while (this.buf.length >= 4) {
      const length = new DataView(this.buf.buffer, this.buf.byteOffset, 4).getUint32(0, false);
      if (length > MAX_FRAME_BYTES) throw new ProtocolError(`frame length ${length} exceeds ${MAX_FRAME_BYTES}`);
      if (this.buf.length < 4 + length) break;
      const raw = this.buf.slice(4, 4 + length);
      this.buf = this.buf.slice(4 + length);
      let parsed: unknown;
      try {
        parsed = JSON.parse(decoder.decode(raw));
      } catch (err) {
        throw new ProtocolError(`bad frame: ${(err as Error).message}`);
      }
      if (parsed && typeof parsed === "object") out.push(parsed as GlassesMessage);
    }
    return out;
  }
}

/** Shorten to `limit` characters, ending with an ellipsis when something was cut. */
export function clip(text: string, limit: number): string {
  const clean = String(text ?? "").split(/\s+/).filter(Boolean).join(" ");
  const chars = Array.from(clean);
  if (chars.length <= limit) return clean;
  return chars.slice(0, Math.max(0, limit - 1)).join("").trimEnd() + "…";
}

export function hud(fields: { mode?: HudMode; you?: string; reply?: string; status?: string } = {}): HudCard {
  const mode = fields.mode ?? "info";
  if (!HUD_MODES.includes(mode)) throw new ProtocolError(`unknown HUD mode ${mode}`);
  return {
    type: "hud",
    mode,
    you: clip(fields.you ?? "", HUD_LIMITS.you),
    reply: clip(fields.reply ?? "", HUD_LIMITS.reply),
    status: clip(fields.status ?? "", HUD_LIMITS.status),
  };
}

export const ping = (): GlassesMessage => ({ type: "ping" });

type Lang = "zh" | "en";
const STAGES: Record<Lang, Record<string, string>> = {
  zh: { uploading: "上传中", extracting: "准备视频", queued: "排队中", gemx: "重建身体", ik: "计算关节角度", processing: "分析中", ready: "分析完成" },
  en: { uploading: "Uploading", extracting: "Preparing video", queued: "In line", gemx: "Rebuilding body", ik: "Measuring joints", processing: "Analyzing", ready: "Analysis ready" },
};
const WORDS: Record<Lang, Record<string, string>> = {
  zh: { fix: "要改", noFix: "这次没有明显要改的地方", ahead: "前面 {n} 个" },
  en: { fix: "Fix", noFix: "Nothing big to fix this time", ahead: "{n} ahead" },
};

/** `Rebuilding body 45%`, plus the queue position while the clip waits for the GPU. */
export function progressCard(progress: Progress, lang: Lang = "zh"): HudCard {
  const label = STAGES[lang][String(progress.stage)] ?? STAGES[lang]["processing"];
  const status = typeof progress.percent === "number" ? `${label} ${Math.trunc(progress.percent)}%` : label;
  let reply = "";
  if (typeof progress.jobs_ahead === "number" && progress.jobs_ahead > 0) {
    reply = WORDS[lang]["ahead"].replace("{n}", String(progress.jobs_ahead));
    if (progress.eta_minutes) reply += ` · ~${progress.eta_minutes} min`;
  }
  return hud({ mode: "info", status, reply });
}

/** The main fix: measured change in the status line, headline and feel cue below. */
export function fixCard(finding: Finding | null, lang: Lang = "zh"): HudCard {
  if (!finding) return hud({ mode: "assist", status: STAGES[lang]["ready"], reply: WORDS[lang]["noFix"] });
  const parts = fixSummary(finding);
  const status = WORDS[lang]["fix"] + (parts.metric ? ` ${parts.metric}` : "");
  const reply = parts.cue ? (parts.headline ? `${parts.headline}. ${parts.cue}` : parts.cue) : parts.headline;
  return hud({ mode: "assist", status, reply });
}
