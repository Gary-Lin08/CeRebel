/**
 * Optional checks against a real server; skipped unless CEREBEL_TEST_BASE_URL is set.
 *   CEREBEL_TEST_BASE_URL=http://127.0.0.1:8092 npm test
 * Creates a throwaway account and deletes it at the end. Never point it at production with a real account.
 */
import assert from "node:assert/strict";
import { after, before, describe, test } from "node:test";

import { CerebelClient, mainFix } from "../src/index.ts";

const BASE = process.env["CEREBEL_TEST_BASE_URL"] ?? "";

describe("live server", { skip: !BASE && "set CEREBEL_TEST_BASE_URL to run against a real server" }, () => {
  const client = new CerebelClient({ baseUrl: BASE });
  const email = `sdk-ts-${Math.random().toString(16).slice(2, 10)}@example.com`;

  before(async () => {
    await client.register({ email, password: `pw-${Math.random().toString(36).slice(2)}x`, acceptTerms: true });
  });
  after(async () => {
    await client.deleteAccount().catch(() => undefined);
  });

  test("health, catalog and account", async () => {
    assert.equal((await client.health())["process"], "up");
    const live = (await client.sports()).sports.filter((s) => s["status"] === "live").map((s) => s["id"]);
    assert.ok(live.includes("golf"));
    const me = await client.me();
    assert.equal(me.user?.email, email);
    assert.ok(me.video_consent?.text);
  });

  test("demo report and coach stream", async (t) => {
    const demo = await client.session(undefined, { sample: true });
    if (demo["empty"]) return t.skip("this server has no CEREBEL_DEMO_SESSION_ID");
    const sid = String(demo["session_id"]);
    const report = await client.sportAnalysis(sid);
    assert.ok(Array.isArray(report.findings));
    if (report.findings.length) assert.ok(mainFix(report)?.headline);
    const events = [];
    for await (const e of client.askStream("What's the biggest thing I should fix in this swing?", sid)) events.push(e);
    assert.equal(events.at(-1)?.type, "done");
  });
});
