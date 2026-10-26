# CeRebel Playground

Primary product UI: `motion_agent/app/ui/index.html` on `chat_server` `/`.

## Default vs consumer

| URL | Mode |
|-----|------|
| `/` | Landing for signed-out visitors; **Playground** with `?dev=1` (or on the operator port) |
| `/app` | **Customer app**: home, swing report, coach sheet, upload, account (`app.html`, `app.js`, `app.css`) |
| `/?consumer=1` | Slim To-C shell (fix-first hero, sports catalog, account/library) |

## Customer app (`/app`)

- Routes in the hash: `#/` home, `#/swing/<id>` (optionally `/drill` or `/data`), `#/sample`, plus `#signin` / `#signup`.
- Signed-out visitors land on the sample report; the landing's sign-in and upload buttons open `/app`.
- `/` sends signed-in customers, email links (`/?session=<id>`) and the checkout return to `/app`.
  `CEREBEL_APP_UI=classic` turns that off and keeps everyone on the old single page.
- Reuses `skeleton-overlay.js`, `compare.js`, `billing.js` and `swing_trim.js` through the same globals the old page defined.
- Not in the app yet: the full-screen 3D body view, and the live voice call (needs `cerebelVoice.start()` from `voice-coach.js`;
  until then the mic button dictates a question where the browser supports it).
- Tests: `motion_agent/tests/test_app_page.py`.

## Playground surface

- Prompt modes: general / snowboarding / golf / baseball (clamped honestly per capture)
- Session load / newest processed video
- Capture player + ego mesh + golf phase board + sport analysis board
- Coach chat with KB-scoped RAG (`sport_sources.py`)
- Account + library still available in the nav for uploads
- Sport pack catalog visible; live vs coming-soon stays honest

## Related

- Sport Pack KBs: `knowledge/corpus/<sport>/`
- Registry: `sports/registry.py`
- Optional consumer notes: historical ToC experiments live behind `?consumer=1` only
