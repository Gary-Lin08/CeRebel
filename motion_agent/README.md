# Motion Agent — Tool-Augmented Human Motion Comprehension Demo

Proof-of-concept: **Qwen3-VL** reasons about human movement using deterministic
motion/OpenSim tools on top of the existing CeRebel GEM-X → OpenSim pipeline.

The VLM is **not** the source of truth for numbers. Tools backed by
`opensim-motion.json` and `estimated-dynamics.json` are.

## Architecture

```
newest session (data/latest_mesh.json)
        │
        ▼
 MotionSequence adapter (SMPL-X names + OpenSim/SOMA aliases)
        │
        ▼
 motion evidence tools  ←── Qwen3-VL tool loop ──→ chat UI (:8092)
```

Package layout: `motion_agent/` (motion, dynamics, tools, llm, app, tests).

## Requirements

- Existing preprocessed sessions under `data/sessions/<id>/ik/`
- Python 3.10+ with `numpy`, `openai`
- DashScope API key in `~/Qwen_KEY.txt` (`Qwen_API_KEY=...`) for Qwen3-VL

Local GPU weights are **not** required for the default backend (DashScope intl).

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `MOTION_AGENT_BACKEND` | `dashscope` | `dashscope` / `mock` |
| `MOTION_AGENT_MODEL` | `qwen3.7-plus` | Model id |
| `MOTION_AGENT_BASE_URL` | DashScope CN compatible-mode | OpenAI-compatible base URL |
| `MOTION_AGENT_MAX_TOOL_CALLS` | unlimited | Optional positive cap; unset/`0`/`none` = no limit |
| `MOTION_AGENT_PORT` | `8092` | Demo UI/API port |
| `CEREBEL_DATA_DIR` | `.../CerebelServer/data` | Session root |
| `DASHSCOPE_API_KEY` | from `~/Qwen_KEY.txt` | Auth |

## Start the demo

```bash
export DASHSCOPE_API_KEY="$(sed -n 's/^Qwen_API_KEY=//p' ~/Qwen_KEY.txt)"
export PYTHONPATH=/root/CerebelServer
python3 -m motion_agent.app.chat_server
```

Open `http://<host>:8092/`. The UI loads the newest ready session from
`data/latest_mesh.json`, shows `ik/session.mp4`, and exposes example questions.

Offline / CI without an API key:

```bash
MOTION_AGENT_BACKEND=mock PYTHONPATH=/root/CerebelServer python3 -m motion_agent.app.chat_server
```

## Motion tools

| Tool | Role |
|------|------|
| `get_motion_state` | Pose / COM / contact at a timestamp |
| `get_motion_events` | Deterministic extrema / contact / jump candidates |
| `get_joint_trajectory` | Angle/velocity stats over an interval |
| `get_com_trajectory` | COM kinematics |
| `get_contact_state` | Foot contact intervals |
| `get_dynamics_summary` | Moments / power / peak GRF |
| `get_joint_dynamics` | Per-joint moment/force/power series |
| `get_ground_reaction_forces` | Estimated GRF |
| `get_joint_power` | `P = τ · ω` |
| `render_force_visualization` | Sagittal skeleton + estimated GRF/moments PNG |
| `get_rgb_frame` / `get_rgb_clip` | Visual evidence |
| `compare_motion` / `search_motion` | Segment compare / heuristic search |
| `search_knowledge_base` / `list_knowledge_sources` | Local RAG over on-disk `.txt` packs (top-k chunks only) |

Set `include_plot=true` on `get_joint_trajectory` / `get_com_trajectory` for PNG time-series.

### Local knowledge base (RAG)

- Corpus: `motion_agent/knowledge/corpus/*.txt` (override with `MOTION_AGENT_KB_CORPUS_DIR`)
- Index cache: `motion_agent/.cache/kb_index/` (`MOTION_AGENT_KB_INDEX_DIR`)
- Embeddings: DashScope OpenAI-compatible `text-embedding-v3` by default
- Rebuild:

```bash
PYTHONPATH=/root/CerebelServer python3 motion_agent/scripts/rebuild_knowledge_index.py
```

Whole files are never uploaded to Qwen; only retrieved passages are returned by the tool.

### Orientation frames (flips)

| Signal | Frame | Use for |
|--------|-------|---------|
| `pelvis_tilt` OpenSim DOF | generalized coord (can stall near ±180°) | avoid for aerial flips |
| `root_euler_*` / `root_cumulative_rotation_*` | **world** (OpenSim Y-up) | flip *extent* |
| `intrinsic_somersault_*` / `flip_direction_hint` | **rider-intrinsic** (about rightward = facing × up) | front vs back (`+` backflip, `−` frontflip) |

Do not infer frontflip vs backflip from world Euler sign alone.

Power is derived: `flexionMomentNm * angularVelocityRadS`. Dynamics are
**estimated** (monocular GRF), with confidence ceilings from OpenSim export.

## Tests

```bash
PYTHONPATH=/root/CerebelServer python3 -m unittest discover -s motion_agent/tests -p 'test_*.py' -v
```

## Known limitations

- Upstream pose is GEM-X **SOMA77**, retargeted to OpenSim Rajagopal — not native SMPL-X mesh kinematics.
- `motion-data.json` angle stubs are ignored; use OpenSim coordinates.
- No force plates / CoP in the export.
- Default VLM path is cloud DashScope (video via data URI or keyframe fallback).
- `render_force_visualization` is an OpenSim stick-figure overlay, not a full SOMA/SMPL-X mesh.
- Local vLLM/SGLang serving is a graceful stub until weights are configured (`MOTION_AGENT_BACKEND=local`).
- Not a production UI; standalone demo only.
