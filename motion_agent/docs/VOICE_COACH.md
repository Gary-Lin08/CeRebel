# Live voice coach (prototype)

A phone-call style conversation with the coach: the user talks, the coach answers out loud, and the
user can talk over the coach to interrupt. Off by default (`CEREBEL_VOICE_ENABLED=0`).

## How it works

```
browser mic ──16 kHz PCM──▶ /api/voice/live (WebSocket, chat_server) ──▶ streaming recognition
                                   │                                        │ final sentence
                                   │        precomputed answer? ◀───────────┘
                                   │            │ no
                                   │   "Let me look at that." (cached audio) + coach agent, voice mode
                                   │            │ SAY: spoken text   SCREEN: measured numbers
                                   ◀──24 kHz PCM── streaming speech, sentence by sentence
```

- **Same coach as the text chat.** `answer_question(..., voice=True, history=...)` runs the tool-grounded
  agent with: thinking explicitly off, no picture tools, at most 2 tool rounds, the last 4 turns of the
  call as history, and `VOICE_APPENDIX` (prompts.py) asking for `SAY:` / `SCREEN:`.
- **No numbers out loud.** Speech is plain coaching language. Any spoken sentence that still carries a
  measurement (digits, or "eight centimeters") moves to the screen (`voice.plan_reply`).
- **Numbers on screen are measured values.** The report's own metric for the topic comes first
  (`topic_metrics`, the same value as the fix card). Coach-written lines are dropped when they hedge
  ("about", "~", "estimated"), carry no number, or contain a number absent from this turn's tool output
  (`screen_line_problem`; allows rounding to the shown precision, tens, and m→cm, s→ms, rad→deg).
- **Precomputed answers** (coach briefs) are spoken without a model call when the spoken question matches
  one and does not ask for something specific ("how fast", "drill", "why"…): `match_voice_brief`.
- **Barge-in.** While the coach is talking the browser sends silence, unless the user's voice clearly rises
  over it for 200 ms; then playback stops, `interrupt` is sent with 300 ms of pre-roll so the first word is
  kept, and the server cancels the turn (audio carries a turn id so late chunks are dropped).
- **Limits.** `CEREBEL_VOICE_PLAN=trial`: signed-in free accounts get `CEREBEL_VOICE_TRIAL_S` once, Pro
  gets `CEREBEL_VOICE_PRO_MONTHLY_S` per month (`product/voice_usage.json`), every call is capped at
  `CEREBEL_VOICE_MAX_CALL_S`, hangs up after `CEREBEL_VOICE_IDLE_S` of silence, and each coach turn counts
  against the existing daily question cap.
- **Privacy.** Audio is never written to disk. Transcripts live in the call's memory and the page's chat.
  Events (`voice_call_start` / `voice_call_end`) hold durations, counts and latencies only.

Speech provider: **Together AI** (United States; already the coach's processor in the privacy
policy). Recognition `openai/whisper-large-v3` over `wss://api.together.ai/v1/realtime` with its own
voice-activity detection (600 ms pause ends a turn; segments up to 30 s); synthesis
`hexgrad/Kokoro-82M`, voice `af_heart`, 24 kHz PCM over `/v1/audio/speech/websocket`, one context per
sentence. `mock` stands in for tests. The voice buttons stay hidden until the provider has its key.

Files: `app/ws.py` (standard-library WebSocket), `app/speech.py` (providers: `together`, `mock`),
`app/voice.py` (call, reply rules, usage), `app/chat_server.py` (`/api/voice/live`, `/api/voice/status`),
`ui/voice-coach.js` + `.css` (buttons, consent, call sheet, capture, playback), `scripts/voice_probe.py`.

## Run it locally

```bash
CEREBEL_VOICE_ENABLED=1 CEREBEL_VOICE_PLAN=all CEREBEL_VOICE_PROVIDER=mock \
  MOTION_AGENT_BACKEND=mock python -m motion_agent.app.chat_server        # no keys needed
```

With real speech: put the Together AI key in the environment as `CEREBEL_VOICE_API_KEY` (server
only; `.env.product` on the server), keep `CEREBEL_VOICE_PROVIDER=together`. Time a call end to end:

```bash
python motion_agent/scripts/voice_probe.py ws://127.0.0.1:8092/api/voice/live \
  --say "What's the biggest thing I should fix?" --say "What drill should I do for that?" [--barge-in]
```

Tests: `python -m unittest motion_agent.tests.test_voice`.

## Measured (2026-10-26)

Not yet on Together: there is no key on the development machine. These end-to-end numbers come from
the same pipeline run against Model Studio streaming speech in Beijing from a Mac in the US (that
adapter was removed afterwards because the region does not fit the privacy policy), with the coach on
Qwen3.7-Plus in Beijing. Re-measure on Together from the production server with `voice_probe.py`.

| Moment, from the end of the user's speech | Time |
|---|---|
| Transcript on screen | 0.84–1.2 s |
| Precomputed answer starts speaking | 1.8–1.9 s |
| "Let me look at that." starts (coach turns) | 1.1–1.6 s |
| Coach answer starts speaking | 5.7–10.4 s (one model round ≈ 2.5 s, each tool round +2.6–3.8 s) |
| Audio of an interrupted turn arriving after `interrupt` | 0 chunks |

## Cost per minute (Together list prices, October 2026)

About 2.4 turns a minute; a coach turn reads ≈24.6k input tokens (17.3k without a tool round, 35.5k
with one, measured) and speaks ≈250 characters. Precomputed answers and the opener cost no model call.

| Part | Price | Per minute |
|---|---|---|
| Recognition, Whisper large v3 streaming | $0.0035 / audio minute (Nemotron streaming: $0.0015) | $0.0035 |
| Coach, Qwen3.7-Plus | $0.32 / 1M in, $1.28 / 1M out | ≈ $0.019 |
| Speech, Kokoro | $4 / 1M characters | ≈ $0.0024 |
| **Total** | | **≈ $0.025 (≈ $1.50 an hour)** |

Pro's 60 minutes a month cost at most ≈ $1.50; a free 3-minute try ≈ $0.08. Cutting the voice context
to ≈6k tokens would bring the total to ≈ $0.014 a minute. Together does not say whether streaming
recognition bills open-socket time or audio sent; the call streams the whole time, so assume call time.

## Before launch

1. Together AI key as `CEREBEL_VOICE_API_KEY`; one real call to confirm the adapter (it is written to the
   documented protocol and tested against a local stand-in only), then p50/p95 from the production server
   with `voice_probe.py`.
2. Written confirmation from Together (DPA): audio and text processed in the United States (their docs
   say `api.together.ai` does not guarantee a region, and the US-only endpoint lists chat models only),
   retention or zero retention, no training on customer data.
3. Privacy policy and consent text (draft below), reviewed by counsel; bump a consent version server-side.
4. Real-device tests: iOS Safari and Android Chrome (echo cancellation, earpiece vs speaker, Bluetooth,
   lock screen, volume while the mic is open), desktop Chrome/Safari/Firefox.
5. WebSocket through the Cloudflare tunnel; behind cerebel.tech/playground set `CEREBEL_VOICE_ORIGIN`.
6. Shorter voice context (≈17k tokens today; −0.5 s and about −60% model cost at ≈6k) and streaming the
   final model round (≈−0.4 s).
7. Dashboards from `voice_call_end`: latency, interruptions, dropped screen lines, cost per minute.

### Privacy policy changes (draft for counsel; `ui/legal/privacy.html`)

- §1 What we collect, new item after "Coach questions": "**Live coach calls.** While a call is on, the
  audio from your microphone is streamed to our speech provider to turn it into text, and the coach's
  replies are turned into speech. We do not record or keep the audio, and we do not keep a transcript on
  our servers; we record only that a call took place (time, length, account and swing)."
- §4 How we use your data: "answer your questions" → "answer your questions, in writing or out loud in a
  live coach call"; add "We do not use your voice to identify you or to create a voiceprint."
- §6 Together AI row: "AI coach: receives your question and the computed numbers, not your video or its
  frames" → "AI coach and live coach calls: receives your questions (typed, or your voice during a call),
  the computed numbers, and the coach's replies to read aloud; not your video or its frames".
- §7: "We store and process your videos, body data and account data only in the United States" → "…your
  videos, body data, account data and call audio only in the United States" (once Together confirms).
- §8 new row: "Live coach call audio and transcripts — Not stored by us. Audio is streamed for
  transcription and discarded as the call goes; the transcript stays in your open page. A record that a
  call took place is kept like other usage events."
- §11 Texas: keep "We do not capture … voiceprints" (true: no speaker identification is enabled).
- §14 CCPA: "physical characteristics (videos and body data)" → "physical characteristics (videos and
  body data), audio information (live coach calls)".
- Hold to talk uses the browser's own speech recognition (Google in Chrome, Apple in Safari). While live
  coaching is on, that button is hidden; if it stays in use, disclose it in §6.
- Voice consent card: name the provider and country the way the video consent does ("Together AI, United
  States") and record the consent version server-side, not only in the browser.
