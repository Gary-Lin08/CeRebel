import assert from "node:assert/strict";
import { connect, createServer } from "node:net";
import { test } from "node:test";

import { glasses } from "../src/index.ts";

const { FrameDecoder, encodeFrame, fixCard, hud, progressCard, MAX_FRAME_BYTES, ProtocolError } = glasses;

test("frames round-trip in any chunking", () => {
  const a = encodeFrame({ type: "hello", fw: "1.0" });
  const b = encodeFrame(hud({ status: "挥杆分析", reply: "保持后膝微屈" }));
  const all = new Uint8Array(a.length + b.length);
  all.set(a);
  all.set(b, a.length);
  const decoder = new FrameDecoder();
  const out = [];
  for (let i = 0; i < all.length; i++) out.push(...decoder.feed(all.slice(i, i + 1)));
  assert.deepEqual(
    out.map((m) => m.type),
    ["hello", "hud"],
  );
  assert.equal(out[1]["reply"], "保持后膝微屈");
  assert.equal(new DataView(a.buffer).getUint32(0, false), a.length - 4);
});

test("oversized frames and unknown modes are rejected", () => {
  const head = new Uint8Array(4);
  new DataView(head.buffer).setUint32(0, MAX_FRAME_BYTES + 1, false);
  assert.throws(() => new FrameDecoder().feed(head), ProtocolError);
  assert.throws(() => hud({ mode: "party" as never }), ProtocolError);
});

test("cards clip fields and match the Python SDK wording", () => {
  const card = hud({ status: "x".repeat(100), reply: "y".repeat(500) });
  assert.equal(Array.from(card.status).length, 40);
  assert.equal(Array.from(card.reply).length, 160);
  assert.ok(card.reply.endsWith("…"));
  const fix = fixCard({ id: "k", headline: "Your back knee locks too early", cue: "Keep a soft back knee", metric: { value: "66° → 0°" } });
  assert.equal(fix.status, "要改 66° → 0°");
  assert.equal(fix.reply, "Your back knee locks too early. Keep a soft back knee");
  assert.equal(progressCard({ session_id: "S", stage: "queued", percent: 22, jobs_ahead: 2, eta_minutes: 4 }, "en").reply, "2 ahead · ~4 min");
});

test("works over a real TCP socket (glasses side mocked with node:net)", async () => {
  const received: unknown[] = [];
  const server = createServer((sock) => {
    const decoder = new FrameDecoder();
    sock.write(encodeFrame({ type: "hello", fw: "node-mock", battery: 70 }));
    sock.on("data", (chunk) => {
      for (const m of decoder.feed(new Uint8Array(chunk))) {
        received.push(m);
        sock.end();
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as { port: number }).port;
  const hello = await new Promise<Record<string, unknown>>((resolve, reject) => {
    const sock = connect(port, "127.0.0.1");
    const decoder = new FrameDecoder();
    sock.on("data", (chunk) => {
      const [m] = decoder.feed(new Uint8Array(chunk));
      if (m) {
        sock.write(encodeFrame(fixCard(null, "en")));
        resolve(m);
      }
    });
    sock.on("error", reject);
  });
  await new Promise((r) => setTimeout(r, 50));
  server.close();
  assert.equal(hello["fw"], "node-mock");
  assert.equal((received[0] as Record<string, unknown>)["reply"], "Nothing big to fix this time");
});
