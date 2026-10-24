#!/usr/bin/env bash
# Background launcher. Prefer ./run.sh for live progress.
#
# Flags: --fake-egocentric / --fake-ego, --no-fake-egocentric, -h/--help
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

usage() {
  echo "Usage: $0 [--fake-egocentric] [--no-fake-egocentric]"
  echo "  --fake-egocentric  Hide raw input video; show GEM-X SOMA mesh as the device feed"
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    --fake-egocentric|--fake-ego) export CEREBEL_FAKE_EGOCENTRIC=1 ;;
    --no-fake-egocentric|--no-fake-ego) export CEREBEL_FAKE_EGOCENTRIC=0 ;;
    *) echo "unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

export CEREBEL_HOST="${CEREBEL_HOST:-0.0.0.0}"
export CEREBEL_PORT="${CEREBEL_PORT:-61016}"
export CEREBEL_DATA_DIR="${CEREBEL_DATA_DIR:-$ROOT/data}"
export CEREBEL_GEMX_ROOT="${CEREBEL_GEMX_ROOT:-/root/GEM-X}"
export CEREBEL_GEMX_PYTHON="${CEREBEL_GEMX_PYTHON:-$CEREBEL_GEMX_ROOT/.venv/bin/python}"
export CEREBEL_GEMX_ENABLED="${CEREBEL_GEMX_ENABLED:-1}"
export CEREBEL_GEMX_WORKER_HOST="${CEREBEL_GEMX_WORKER_HOST:-127.0.0.1}"
export CEREBEL_GEMX_WORKER_PORT="${CEREBEL_GEMX_WORKER_PORT:-61017}"
export CEREBEL_VISER_HOST="${CEREBEL_VISER_HOST:-127.0.0.1}"
export CEREBEL_VISER_PORT="${CEREBEL_VISER_PORT:-8090}"
export CEREBEL_KINETIC_LENS_HOST="${CEREBEL_KINETIC_LENS_HOST:-127.0.0.1}"
export CEREBEL_KINETIC_LENS_PORT="${CEREBEL_KINETIC_LENS_PORT:-8091}"
export CEREBEL_IK_ENABLED="${CEREBEL_IK_ENABLED:-1}"
export CEREBEL_IK_PYTHON="${CEREBEL_IK_PYTHON:-$ROOT/.venv-ik/bin/python}"
export CEREBEL_VITPOSE_BATCH="${CEREBEL_VITPOSE_BATCH:-96}"
export CEREBEL_SAM3D_BATCH="${CEREBEL_SAM3D_BATCH:-48}"
export CEREBEL_YOLO_BATCH="${CEREBEL_YOLO_BATCH:-16}"
export CEREBEL_USE_BYTETRACK="${CEREBEL_USE_BYTETRACK:-0}"
export CEREBEL_BATCH_AUTOTUNE="${CEREBEL_BATCH_AUTOTUNE:-1}"
export CEREBEL_VITPOSE_FLIP="${CEREBEL_VITPOSE_FLIP:-1}"
export CEREBEL_FAKE_EGOCENTRIC="${CEREBEL_FAKE_EGOCENTRIC:-0}"
export CEREBEL_SOMA_DISPLAY_SPACE="${CEREBEL_SOMA_DISPLAY_SPACE:-global}"
export PYTHONUNBUFFERED=1

mkdir -p "$CEREBEL_DATA_DIR"

if [[ "${CEREBEL_FAKE_EGOCENTRIC}" == "1" ]]; then
  echo "fake-egocentric ON — raw input video hidden; SOMA mesh shown as device feed"
fi

for pid in $(pgrep -f "/root/CerebelServer/kinetic_lens_viewer.py" || true); do kill "$pid" 2>/dev/null || true; done
for pid in $(pgrep -f "/root/CerebelServer/mesh_viewer_service.py" || true); do kill "$pid" 2>/dev/null || true; done
for pid in $(pgrep -f "/root/CerebelServer/gemx_warm_worker.py" || true); do kill "$pid" 2>/dev/null || true; done
for pid in $(pgrep -f "/root/CerebelServer/server.py" || true); do kill "$pid" 2>/dev/null || true; done
sleep 0.5

if [[ -x "$CEREBEL_GEMX_PYTHON" ]]; then
  nohup "$CEREBEL_GEMX_PYTHON" -u "$ROOT/gemx_warm_worker.py" \
    >> "$ROOT/gemx_worker.log" 2>&1 < /dev/null &
  echo "warm worker pid $! on ${CEREBEL_GEMX_WORKER_HOST}:${CEREBEL_GEMX_WORKER_PORT}"

  nohup "$CEREBEL_GEMX_PYTHON" -u "$ROOT/mesh_viewer_service.py" \
    >> "$ROOT/mesh_viewer.log" 2>&1 < /dev/null &
  echo "mesh viewer pid $! on ${CEREBEL_VISER_HOST}:${CEREBEL_VISER_PORT}"

  if [[ "${CEREBEL_IK_ENABLED:-1}" != "0" ]]; then
    nohup "$ROOT/.venv/bin/python" -u "$ROOT/kinetic_lens_viewer.py" \
      >> "$ROOT/kinetic_lens.log" 2>&1 < /dev/null &
    echo "kinetic lens viewer pid $! on ${CEREBEL_KINETIC_LENS_HOST}:${CEREBEL_KINETIC_LENS_PORT}"
  fi
fi

nohup "$ROOT/.venv/bin/python" -u "$ROOT/server.py" >> "$ROOT/server.log" 2>&1 < /dev/null &
echo "started pid $! on ${CEREBEL_HOST}:${CEREBEL_PORT}"
echo "tip: ./run.sh for foreground logs; warm worker preloads models once"
