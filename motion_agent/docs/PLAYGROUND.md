# CeRebel Playground

Primary product UI: `motion_agent/app/ui/index.html` on `chat_server` `/`.

## Default vs consumer

| URL | Mode |
|-----|------|
| `/` | **Playground** (full chrome: modes, system prompt, capture panels, OpenSim, coach) |
| `/?consumer=1` | Slim To-C shell (fix-first hero, sports catalog, account/library) |

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
