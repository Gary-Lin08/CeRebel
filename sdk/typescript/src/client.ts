import { FAILED_STAGES, TERMINAL_STAGES, mainFix } from "./analysis.ts";
import { AnalysisFailed, CerebelError, errorFor } from "./errors.ts";
import { readSse } from "./sse.ts";
import type {
  AnalyzeResult,
  AuthResult,
  ChatAnswer,
  CoachEvent,
  Finding,
  Library,
  Me,
  Progress,
  SportAnalysis,
  UploadStarted,
} from "./types.ts";

export const DEFAULT_BASE_URL = "https://cerebel.tech/playground";
export const SDK_VERSION = "0.1.0";
/**
 * The pipeline writes "ready" first; sport detection then stamps `sport` (or turns it into
 * "no_swing") a few seconds later. Wait this long for that stamp before accepting a bare "ready".
 */
const FINALIZE_GRACE_MS = 90_000;

type Json = Record<string, unknown>;
type Query = Record<string, string | number | boolean | null | undefined>;

export interface ClientOptions {
  /** May carry a path prefix (https://cerebel.tech/playground); it is kept on every call. */
  baseUrl?: string;
  token?: string | null;
  /** Origin for uploads when the app sits behind a proxy with a body limit (CEREBEL_UPLOAD_ORIGIN). */
  uploadBaseUrl?: string;
  /** Per-request limit for JSON calls. Uploads and coach streams are not limited by it. */
  timeoutMs?: number;
  /** Custom fetch (tests, React Native polyfills). Defaults to the global fetch. */
  fetch?: typeof fetch;
}

export interface WaitOptions {
  timeoutMs?: number;
  pollIntervalMs?: number;
  onProgress?: (progress: Progress) => void;
  signal?: AbortSignal;
}

export interface UploadOptions {
  filename?: string;
  /** Keep only this window of the clip, in seconds. */
  trim?: [number, number];
  signal?: AbortSignal;
}

export interface ReportOptions {
  sport?: string;
  handedness?: "right" | "left";
}

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(signal.reason ?? new Error("aborted"));
      },
      { once: true },
    );
  });

/**
 * Client for the Cerebel product API (motion_agent/app/chat_server.py).
 * Every method resolves to the decoded JSON body and rejects with a CerebelError subclass.
 */
export class CerebelClient {
  readonly baseUrl: string;
  readonly uploadBaseUrl: string;
  token: string | null;
  timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.uploadBaseUrl = (options.uploadBaseUrl ?? this.baseUrl).replace(/\/+$/, "");
    this.token = options.token ?? null;
    this.timeoutMs = options.timeoutMs ?? 30_000;
    const f = options.fetch ?? globalThis.fetch;
    if (!f) throw new Error("No fetch available: pass options.fetch");
    this.fetchImpl = f.bind(globalThis);
  }

  // ---------------------------------------------------------------- plumbing

  private url(path: string, query?: Query, base = this.baseUrl): string {
    let url = base + path;
    if (query) {
      const params = new URLSearchParams();
      for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null) params.set(k, String(v));
      const qs = params.toString();
      if (qs) url += "?" + qs;
    }
    return url;
  }

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    const h: Record<string, string> = { Accept: "application/json", ...extra };
    if (this.token) h["Authorization"] = `Bearer ${this.token}`;
    return h;
  }

  private async send(url: string, init: RequestInit, allow: number[] = []): Promise<Response> {
    let resp: Response;
    try {
      resp = await this.fetchImpl(url, init);
    } catch (err) {
      if ((err as Error)?.name === "AbortError" || (err as Error)?.name === "TimeoutError") {
        throw new CerebelError("The Cerebel API did not answer in time.");
      }
      throw new CerebelError(`Could not reach the Cerebel API: ${(err as Error)?.message ?? err}`);
    }
    if (!resp.ok && !allow.includes(resp.status)) {
      let payload: unknown = {};
      try {
        payload = await resp.json();
      } catch {
        payload = {};
      }
      throw errorFor(resp.status, payload);
    }
    return resp;
  }

  private timeoutSignal(signal?: AbortSignal): AbortSignal | undefined {
    const timeout = this.timeoutMs > 0 && typeof AbortSignal.timeout === "function" ? AbortSignal.timeout(this.timeoutMs) : undefined;
    if (signal && timeout && typeof AbortSignal.any === "function") return AbortSignal.any([signal, timeout]);
    return signal ?? timeout;
  }

  private async get<T = Json>(path: string, query?: Query, allow: number[] = []): Promise<T> {
    const resp = await this.send(this.url(path, query), { method: "GET", headers: this.headers(), signal: this.timeoutSignal() }, allow);
    return (await resp.json()) as T;
  }

  private async post<T = Json>(path: string, body: Json = {}): Promise<T> {
    const resp = await this.send(this.url(path), {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      signal: this.timeoutSignal(),
    });
    return (await resp.json()) as T;
  }

  // ---------------------------------------------------------------- service

  /** Readiness of disk, model and GPU. Resolves for 200 and 503 alike: check `ok` and `gpu.state`. */
  health(): Promise<Json & { ok: boolean; gpu?: { ok: boolean; state: string } }> {
    return this.get("/api/health", undefined, [503]);
  }

  /** Sport Pack catalog: live, beta and coming-soon sports. */
  sports(): Promise<Json & { sports: Json[] }> {
    return this.get("/api/sports");
  }

  /** Plans and, when signed in, this account's quota and subscription. */
  plans(): Promise<Json> {
    return this.get("/api/billing");
  }

  // ---------------------------------------------------------------- account

  /** Create an account and sign in. `acceptTerms` must be true: the person read /terms and /privacy. */
  async register(input: { email: string; password: string; acceptTerms: boolean; coachCode?: string }): Promise<AuthResult> {
    const out = await this.post<AuthResult>("/api/auth/register", {
      email: input.email,
      password: input.password,
      accept_terms: input.acceptTerms === true,
      coach_code: input.coachCode ?? null,
    });
    this.token = out.token ?? this.token;
    return out;
  }

  async login(email: string, password: string): Promise<AuthResult> {
    const out = await this.post<AuthResult>("/api/auth/login", { email, password });
    this.token = out.token ?? this.token;
    return out;
  }

  async logout(): Promise<Json> {
    const out = await this.post("/api/auth/logout");
    this.token = null;
    return out;
  }

  /** Account, captures, quota and video-consent status (`user` is null when signed out). */
  me(): Promise<Me> {
    return this.get<Me>("/api/me");
  }

  /** Same as `me()` but rejects with AuthError when signed out. */
  library(): Promise<Library> {
    return this.get<Library>("/api/library");
  }

  /** Record agreement to the video-processing text (show `me().video_consent.text` first). */
  async acceptVideoConsent(version?: string): Promise<Json> {
    const v = version ?? String((await this.me()).video_consent?.version ?? "");
    return this.post("/api/account/video-consent", { accept: true, version: v });
  }

  /** Delete the account and all its captures. Rejects with 409 while a Pro plan still renews. */
  deleteAccount(): Promise<Json> {
    return this.post("/api/account/delete");
  }

  /** Stripe Checkout link for "founding", "annual" or "monthly". */
  checkout(plan = "annual"): Promise<{ ok: true; url: string; id: string }> {
    return this.post("/api/billing/checkout", { plan });
  }

  billingPortal(): Promise<{ ok: true; url: string }> {
    return this.post("/api/billing/portal");
  }

  /** Record one product event (only names in the server's CLIENT_EVENTS list are kept). */
  track(event: string, props: Record<string, string | number | boolean> = {}, visitor?: string): Promise<Json> {
    return this.post("/api/events", { event, props, visitor: visitor ?? null });
  }

  // ---------------------------------------------------------------- captures

  /**
   * Upload one video (MP4/MOV/WebM…, 1.5–30 s, ≥24 fps, short side ≥320 px). Resolves at once
   * with the session id; processing continues on the server.
   * In Node, read a file with `await fs.openAsBlob(path)`.
   */
  async upload(file: Blob, options: UploadOptions = {}): Promise<UploadStarted> {
    const form = new FormData();
    if (options.trim) {
      form.set("trim_start", options.trim[0].toFixed(3));
      form.set("trim_end", options.trim[1].toFixed(3));
    }
    const name = options.filename ?? ((file as File).name || "video.mp4");
    form.set("file", file, name);
    const resp = await this.send(this.url("/api/upload", undefined, this.uploadBaseUrl), {
      method: "POST",
      headers: this.headers(),
      body: form,
      signal: options.signal,
    });
    return (await resp.json()) as UploadStarted;
  }

  /** Stage, percent, message, and the queue position while the clip waits for the GPU. */
  progress(sessionId: string): Promise<Progress> {
    return this.get<Progress>("/api/progress", { session_id: sessionId });
  }

  /**
   * Poll until the capture reaches a final stage. Resolves with the final progress for `ready`;
   * rejects with AnalysisFailed for `error`, `no_swing` or `rejected`.
   */
  async waitForAnalysis(sessionId: string, options: WaitOptions = {}): Promise<Progress> {
    const timeoutMs = options.timeoutMs ?? 45 * 60_000;
    const poll = options.pollIntervalMs ?? 5_000;
    const deadline = Date.now() + timeoutMs;
    let readySince: number | null = null;
    let lastSeen = "";
    let transient = 0;
    while (true) {
      let prog: Progress;
      try {
        prog = await this.progress(sessionId);
        transient = 0;
      } catch (err) {
        // A restart or a dropped connection should not end a 10-minute wait.
        if (err instanceof CerebelError && [0, 502, 503, 504].includes(err.status) && transient < 5) {
          transient += 1;
          await sleep(poll, options.signal);
          continue;
        }
        throw err;
      }
      const stage = String(prog.stage ?? "");
      const seen = `${stage}:${prog.percent}`;
      if (options.onProgress && seen !== lastSeen) options.onProgress(prog);
      lastSeen = seen;
      if (FAILED_STAGES.has(stage)) {
        throw new AnalysisFailed(String(prog.message ?? `Analysis ended with ${stage}`), 200, prog);
      }
      if (stage === "ready") {
        if ("sport" in prog) return prog;
        readySince ??= Date.now();
        if (Date.now() - readySince >= FINALIZE_GRACE_MS) return prog;
      } else if (TERMINAL_STAGES.has(stage)) {
        return prog;
      }
      if (Date.now() >= deadline) {
        throw new CerebelError(`Analysis of ${sessionId} did not finish in time`, 0, prog);
      }
      await sleep(poll, options.signal);
    }
  }

  /**
   * Session overview. Without an id: the account's newest ready capture, or the shared demo when
   * signed out. While a capture is processing the answer has `processing: true`.
   */
  session(sessionId?: string, options: { sample?: boolean } = {}): Promise<Json> {
    return this.get("/api/session", { session_id: sessionId, sample: options.sample ? "1" : undefined });
  }

  /** The report: phases, measured traces, landmarks and findings (biggest fix first). */
  sportAnalysis(sessionId: string, options: ReportOptions = {}): Promise<SportAnalysis> {
    return this.get<SportAnalysis>("/api/sport_analysis", {
      session_id: sessionId,
      sport: options.sport ?? "golf",
      handedness: options.handedness ?? "right",
    });
  }

  golfPhases(sessionId: string, maxSwings = 5): Promise<Json> {
    return this.get("/api/golf_phases", { session_id: sessionId, max_swings: maxSwings });
  }

  /** Instant answers to the standard questions, built without the language model. */
  coachBriefs(sessionId: string, sport = "golf"): Promise<Json> {
    return this.get("/api/coach_briefs", { session_id: sessionId, sport });
  }

  async mainFix(sessionId: string, options: ReportOptions = {}): Promise<Finding | null> {
    return mainFix(await this.sportAnalysis(sessionId, options));
  }

  /** Two of this account's swings, phase by phase, with a verdict per metric. */
  compare(before: string, after: string, options: { beforeSwing?: number; afterSwing?: number } = {}): Promise<Json> {
    return this.get("/api/compare", {
      before,
      after,
      before_swing: options.beforeSwing ?? 0,
      after_swing: options.afterSwing ?? 0,
    });
  }

  logPractice(sessionId: string, findingId: string): Promise<Json & { count: number }> {
    return this.post("/api/library/practice", { session_id: sessionId, finding_id: findingId });
  }

  deleteCapture(sessionId: string): Promise<Json> {
    return this.post("/api/library/delete", { session_id: sessionId });
  }

  /** Upload, wait and fetch the report in one call. */
  async analyze(file: Blob, options: UploadOptions & ReportOptions & WaitOptions = {}): Promise<AnalyzeResult> {
    const started = await this.upload(file, options);
    const sessionId = started.session_id;
    options.onProgress?.({ session_id: sessionId, stage: started.stage, percent: null, warnings: started.warnings ?? [] });
    const progress = await this.waitForAnalysis(sessionId, options);
    const analysis = await this.sportAnalysis(sessionId, options);
    return { session_id: sessionId, progress, analysis, main_fix: mainFix(analysis) };
  }

  /**
   * Absolute URL for a `/media/...` path from a payload (already signed by the server).
   * The server adds its own path prefix to these, so they are joined to the origin only.
   */
  mediaUrl(path: string): string {
    if (/^https?:\/\//.test(path)) return path;
    return new URL(this.baseUrl).origin + path;
  }

  // ---------------------------------------------------------------- coach

  /** Ask the AI coach about one capture. Numbers in the answer come from motion tools. */
  ask(question: string, sessionId?: string): Promise<ChatAnswer> {
    return this.post<ChatAnswer>("/api/chat", { question, video_id: sessionId ?? null });
  }

  /** Same as `ask`, streamed. Event types: status, thinking, assistant, tool, final, error, done. */
  async *askStream(question: string, sessionId?: string, signal?: AbortSignal): AsyncGenerator<CoachEvent> {
    const resp = await this.send(this.url("/api/chat/stream"), {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json", Accept: "text/event-stream" }),
      body: JSON.stringify({ question, video_id: sessionId ?? null }),
      signal,
    });
    if (!resp.body) throw new CerebelError("The coach stream had no body");
    for await (const event of readSse(resp.body)) yield event as CoachEvent;
  }
}
