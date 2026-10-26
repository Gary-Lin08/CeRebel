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

Files: `app/ws.py` (standard-library WebSocket), `app/speech.py` (providers: `dashscope`, `mock`),
`app/voice.py` (call, reply rules, usage), `app/chat_server.py` (`/api/voice/live`, `/api/voice/status`),
`ui/voice-coach.js` + `.css` (buttons, consent, call sheet, capture, playback), `scripts/voice_probe.py`.

## Run it locally

```bash
CEREBEL_VOICE_ENABLED=1 CEREBEL_VOICE_PLAN=all CEREBEL_VOICE_PROVIDER=mock \
  MOTION_AGENT_BACKEND=mock python -m motion_agent.app.chat_server        # no keys needed
```

With a Model Studio key (`~/Qwen_KEY.txt`), use `CEREBEL_VOICE_PROVIDER=dashscope` and
`CEREBEL_VOICE_WS_BASE=wss://dashscope.aliyuncs.com` for a Beijing key. Time a call end to end:

```bash
python motion_agent/scripts/voice_probe.py ws://127.0.0.1:8092/api/voice/live \
  --say "What's the biggest thing I should fix?" --say "What drill should I do for that?" [--barge-in]
```

Tests: `python -m unittest motion_agent.tests.test_voice`.

## Measured (2026-10-26, Mac in the US → Beijing region, 600 ms end-of-turn pause)

| Moment, from the end of the user's speech | Time |
|---|---|
| Transcript on screen | 0.84–1.2 s |
| Precomputed answer starts speaking | 1.8–1.9 s |
| "Let me look at that." starts (coach turns) | 1.1–1.6 s |
| Coach answer starts speaking | 5.7–10.4 s (one model round ≈ 2.5 s, each tool round +2.6–3.8 s) |
| Audio of an interrupted turn arriving after `interrupt` | 0 chunks |

## Before launch

1. US speech provider adapter (Together AI realtime STT + TTS, already a listed processor) and a key;
   measure p50/p95 from the production server with `voice_probe.py`. Model Studio speech exists only in
   Singapore and Beijing, which the privacy policy (US only) does not allow.
2. Written confirmation from the provider: processing region, retention / zero retention, no training.
3. Privacy policy and consent text (draft below), reviewed by counsel; bump a consent version server-side.
4. Real-device tests: iOS Safari and Android Chrome (echo cancellation, earpiece vs speaker, Bluetooth,
   lock screen, volume while the mic is open), desktop Chrome/Safari/Firefox.
5. WebSocket through the Cloudflare tunnel; behind cerebel.tech/playground set `CEREBEL_VOICE_ORIGIN`.
6. Shorter voice context (≈17k tokens today; −0.5 s and about −60% model cost at ≈6k) and streaming the
   final model round (≈−0.4 s).
7. Dashboards from `voice_call_end`: latency, interruptions, dropped screen lines, cost per minute.

### Privacy policy draft (for review)

- §1 What we collect: "Live coach: while a call is on, the audio from your microphone and its transcript."
- §4 Use: "to understand your question and answer it out loud. We do not use your voice to identify you."
- §6 Processors: speech provider row — "turns your call audio into text and the coach's reply into speech".
- §8 Retention: "Call audio is not recorded or stored by us. Transcripts appear in your chat and are not
  kept on our servers after the call."
- §14 CCPA categories: add "audio information".
- Hold to talk (browser speech recognition) sends audio to the browser maker (Google in Chrome, Apple in
  Safari): disclose it, or switch that button to our own recognition.
