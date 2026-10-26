import assert from "node:assert/strict";
import { test } from "node:test";

import {
  AnalysisFailed,
  AuthError,
  CerebelClient,
  ConsentRequired,
  PaymentRequired,
  ServiceUnavailable,
  mainFix,
} from "../src/index.ts";

const FINDING = {
  id: "trail-knee",
  headline: "Your back knee locks too early",
  cue: "Keep a soft back knee to the top",
  metric: { value: "66° → 0°" },
  strength: false,
};

type Route = (req: { method: string; url: URL; headers: Headers; body: unknown }) => Response | Promise<Response>;

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** A fetch that answers from `routes` ("GET /api/me") and records every request. */
function fakeFetch(routes: Record<string, Route>) {
  const calls: Array<{ method: string; url: URL; headers: Headers }> = [];
  const impl = (async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input));
    const method = init.method ?? "GET";
    const headers = new Headers(init.headers);
    calls.push({ method, url, headers });
    const route = routes[`${method} ${url.pathname}`];
    if (!route) return json(404, { error: true, message: "not found" });
    const body = typeof init.body === "string" ? JSON.parse(init.body) : init.body;
    return route({ method, url, headers, body });
  }) as typeof fetch;
  return { impl, calls };
}

test("register stores the token and sends it as a bearer header", async () => {
  const { impl, calls } = fakeFetch({
    "POST /playground/api/auth/register": ({ body }) => {
      assert.equal((body as Record<string, unknown>)["accept_terms"], true);
      return json(200, { ok: true, token: "tok-1", user: { user_id: "u1", email: "a@example.com" } });
    },
    "GET /playground/api/me": () => json(200, { ok: true, user: { user_id: "u1", email: "a@example.com" } }),
  });
  const client = new CerebelClient({ baseUrl: "https://example.test/playground/", fetch: impl });
  await client.register({ email: "a@example.com", password: "password123", acceptTerms: true });
  assert.equal(client.token, "tok-1");
  await client.me();
  assert.equal(calls[1].url.pathname, "/playground/api/me");
  assert.equal(calls[1].headers.get("authorization"), "Bearer tok-1");
});

test("error statuses map to typed errors", async () => {
  const { impl } = fakeFetch({
    "POST /api/auth/login": () => json(401, { ok: false, error: true, message: "Email or password is incorrect." }),
    "POST /api/upload": () =>
      json(428, { error: true, consent_required: true, message: "Agree first", video_consent: { version: "v2", text: "…" } }),
    "GET /api/health": () => json(503, { ok: false, gpu: { ok: false, state: "down" } }),
  });
  const client = new CerebelClient({ baseUrl: "https://example.test", fetch: impl });
  await assert.rejects(client.login("a@example.com", "x"), AuthError);
  const err = await client.upload(new Blob(["x"])).catch((e) => e);
  assert.ok(err instanceof ConsentRequired);
  assert.equal(err.consent["version"], "v2");
  assert.equal((await client.health()).gpu?.state, "down");
});

test("402 and 503 on upload", async () => {
  let status = 402;
  const { impl } = fakeFetch({
    "POST /api/upload": () => json(status, { error: true, message: "no", quota: { remaining: 0 } }),
  });
  const client = new CerebelClient({ baseUrl: "https://example.test", token: "t", fetch: impl });
  const paid = await client.upload(new Blob(["x"])).catch((e) => e);
  assert.ok(paid instanceof PaymentRequired);
  assert.equal(paid.quota["remaining"], 0);
  status = 503;
  await assert.rejects(client.upload(new Blob(["x"])), ServiceUnavailable);
});

test("upload sends multipart with trim fields to the upload origin", async () => {
  const { impl, calls } = fakeFetch({
    "POST /api/upload": async ({ body }) => {
      const form = body as FormData;
      assert.equal(form.get("trim_start"), "0.500");
      assert.equal((form.get("file") as File).name, "swing.mp4");
      return json(200, { ok: true, session_id: "S1", stage: "extracting", warnings: [] });
    },
  });
  const client = new CerebelClient({
    baseUrl: "https://cerebel.tech/playground",
    uploadBaseUrl: "https://upload.example.test",
    token: "t",
    fetch: impl,
  });
  const out = await client.upload(new Blob(["video"]), { filename: "swing.mp4", trim: [0.5, 2] });
  assert.equal(out.session_id, "S1");
  assert.equal(calls[0].url.origin + calls[0].url.pathname, "https://upload.example.test/api/upload");
});

test("analyze waits past a bare ready until sport detection stamps it", async () => {
  const stages = [
    { stage: "extracting", percent: 12 },
    { stage: "queued", percent: 22, jobs_ahead: 1 },
    { stage: "gemx", percent: 45 },
    { stage: "ready", percent: 100 },
    { stage: "ready", percent: 100, sport: "golf" },
  ];
  let i = 0;
  const { impl } = fakeFetch({
    "POST /api/upload": () => json(200, { ok: true, session_id: "S1", stage: "extracting", warnings: [] }),
    "GET /api/progress": () => json(200, { session_id: "S1", ...stages[Math.min(i++, stages.length - 1)] }),
    "GET /api/sport_analysis": () => json(200, { error: false, sport: "golf", phases: [], series: [], landmarks: [], findings: [FINDING] }),
  });
  const client = new CerebelClient({ baseUrl: "https://example.test", token: "t", fetch: impl });
  const seen: string[] = [];
  const result = await client.analyze(new Blob(["x"]), { pollIntervalMs: 1, onProgress: (p) => seen.push(String(p.stage)) });
  assert.equal(result.progress.sport, "golf");
  assert.equal(result.main_fix?.id, "trail-knee");
  // The first event is the upload answer itself; then one per stage change.
  assert.deepEqual(seen, ["extracting", "extracting", "queued", "gemx", "ready"]);
});

test("no_swing rejects with AnalysisFailed", async () => {
  const { impl } = fakeFetch({
    "GET /api/progress": () => json(200, { session_id: "S1", stage: "no_swing", percent: 100, message: "No swing" }),
  });
  const client = new CerebelClient({ baseUrl: "https://example.test", token: "t", fetch: impl });
  const err = await client.waitForAnalysis("S1", { pollIntervalMs: 1 }).catch((e) => e);
  assert.ok(err instanceof AnalysisFailed);
  assert.equal(err.stage, "no_swing");
});

test("askStream parses SSE frames split across chunks", async () => {
  const frames = [
    { type: "status", message: "Calling model…" },
    { type: "tool", name: "build_sport_analysis" },
    { type: "done", answer: "Keep a soft back knee." },
  ]
    .map((e) => `data: ${JSON.stringify(e)}\n\n`)
    .join("");
  const bytes = new TextEncoder().encode(frames);
  const { impl } = fakeFetch({
    "POST /api/chat/stream": () =>
      new Response(
        new ReadableStream({
          start(controller) {
            for (let i = 0; i < bytes.length; i += 7) controller.enqueue(bytes.slice(i, i + 7));
            controller.close();
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      ),
  });
  const client = new CerebelClient({ baseUrl: "https://example.test", fetch: impl });
  const events = [];
  for await (const e of client.askStream("fix?", "DEMO")) events.push(e);
  assert.deepEqual(
    events.map((e) => e.type),
    ["status", "tool", "done"],
  );
  assert.equal(events[2].answer, "Keep a soft back knee.");
});

test("mainFix skips strengths and mediaUrl joins the origin", () => {
  assert.equal(mainFix({ findings: [{ id: "good", strength: true }, FINDING] })?.id, "trail-knee");
  assert.equal(mainFix({ findings: [{ id: "good", strength: true }] })?.id, "good");
  assert.equal(mainFix({ findings: [] }), null);
  const client = new CerebelClient({ baseUrl: "https://cerebel.tech/playground", fetch: (() => {}) as unknown as typeof fetch });
  assert.equal(client.mediaUrl("/playground/media/A/session.mp4?mt=1"), "https://cerebel.tech/playground/media/A/session.mp4?mt=1");
});
