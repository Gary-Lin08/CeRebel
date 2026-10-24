#!/usr/bin/env bash
# Foreground launcher: capture + warm GEM-X worker + mesh viewer until Ctrl+C.
#
# Flags:
#   --fake-egocentric / --fake-ego   Hide raw input video; show GEM-X SOMA mesh as the "device" feed
#   --no-fake-egocentric             Disable fake-egocentric mode
#   -h / --help                      Show this help
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

usage() {
  cat <<'EOF'
Foreground launcher: capture + warm GEM-X worker + mesh viewer until Ctrl+C.

Flags:
  --fake-egocentric / --fake-ego   Hide raw input video; show GEM-X SOMA mesh as the device feed
  --no-fake-egocentric             Disable fake-egocentric mode
  -h / --help                      Show this help

Usage: ./run.sh [--fake-egocentric] [--no-fake-egocentric]
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      usage
      exit 0
      ;;
    --fake-egocentric|--fake-ego)
      export CEREBEL_FAKE_EGOCENTRIC=1
      ;;
    --no-fake-egocentric|--no-fake-ego)
      export CEREBEL_FAKE_EGOCENTRIC=0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      usage >&2
      exit 2
      ;;
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
# Push VitPose / SAM3D batch sizes on A100-40G (autotune finds the ceiling).
export CEREBEL_VITPOSE_BATCH="${CEREBEL_VITPOSE_BATCH:-96}"
export CEREBEL_SAM3D_BATCH="${CEREBEL_SAM3D_BATCH:-48}"
export CEREBEL_YOLO_BATCH="${CEREBEL_YOLO_BATCH:-16}"
export CEREBEL_USE_BYTETRACK="${CEREBEL_USE_BYTETRACK:-0}"
export CEREBEL_BATCH_AUTOTUNE="${CEREBEL_BATCH_AUTOTUNE:-1}"
# Set to 0 for ~2x VitPose throughput (skips flip-test TTA).
export CEREBEL_VITPOSE_FLIP="${CEREBEL_VITPOSE_FLIP:-1}"
export CEREBEL_FAKE_EGOCENTRIC="${CEREBEL_FAKE_EGOCENTRIC:-0}"
export CEREBEL_SOMA_DISPLAY_SPACE="${CEREBEL_SOMA_DISPLAY_SPACE:-global}"
export PYTHONUNBUFFERED=1
export CEREBEL_LOG_LEVEL="${CEREBEL_LOG_LEVEL:-INFO}"

mkdir -p "$CEREBEL_DATA_DIR"

if [[ "${CEREBEL_FAKE_EGOCENTRIC}" == "1" ]]; then
  echo "[run] fake-egocentric ON — raw input video hidden; SOMA mesh shown as device feed"
fi

stop_old() {
  local pid
  for pid in $(pgrep -f "/root/CerebelServer/kinetic_lens_viewer.py" || true); do
    kill "$pid" 2>/dev/null || true
  done
  for pid in $(pgrep -f "/root/CerebelServer/mesh_viewer_service.py" || true); do
    kill "$pid" 2>/dev/null || true
  done
  for pid in $(pgrep -f "/root/CerebelServer/gemx_warm_worker.py" || true); do
    kill "$pid" 2>/dev/null || true
  done
  for pid in $(pgrep -f "/root/CerebelServer/server.py" || true); do
    kill "$pid" 2>/dev/null || true
  done
  sleep 0.5
}
stop_old

VIEWER_PID=""
KINETIC_LENS_PID=""
WORKER_PID=""
SERVER_PID=""

cleanup() {
  echo
  echo "shutting down…"
  [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true
  [[ -n "$WORKER_PID" ]] && kill "$WORKER_PID" 2>/dev/null || true
  [[ -n "$VIEWER_PID" ]] && kill "$VIEWER_PID" 2>/dev/null || true
  [[ -n "$KINETIC_LENS_PID" ]] && kill "$KINETIC_LENS_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  wait "$WORKER_PID" 2>/dev/null || true
  wait "$VIEWER_PID" 2>/dev/null || true
  wait "$KINETIC_LENS_PID" 2>/dev/null || true
  echo "stopped"
}
trap cleanup EXIT INT TERM

if [[ -x "$CEREBEL_GEMX_PYTHON" ]]; then
  echo "[run] starting warm GEM-X worker (preload + batch autotune; can take a few minutes)…"
  "$CEREBEL_GEMX_PYTHON" -u "$ROOT/gemx_warm_worker.py" &
  WORKER_PID=$!
  echo "[run] warm worker pid $WORKER_PID  →  ${CEREBEL_GEMX_WORKER_HOST}:${CEREBEL_GEMX_WORKER_PORT}"

  # Wait until models are loaded/tuned before starting the viewer (VRAM contention).
  echo "[run] waiting for warm worker ready…"
  for _ in $(seq 1 180); do
    if "$ROOT/.venv/bin/python" -c "
from gemx_pipeline import _rpc
r=_rpc({'cmd':'ping'}, timeout=3)
raise SystemExit(0 if r.get('ready') else 1)
" 2>/dev/null; then
      echo "[run] warm worker ready"
      break
    fi
    if ! kill -0 "$WORKER_PID" 2>/dev/null; then
      echo "[run] warm worker died during startup" >&2
      exit 1
    fi
    sleep 2
  done

  "$CEREBEL_GEMX_PYTHON" -u "$ROOT/mesh_viewer_service.py" &
  VIEWER_PID=$!
  echo "[run] mesh viewer pid $VIEWER_PID  →  http://0.0.0.0:${CEREBEL_VISER_PORT}"

  if [[ "${CEREBEL_IK_ENABLED:-1}" != "0" ]]; then
    "$ROOT/.venv/bin/python" -u "$ROOT/kinetic_lens_viewer.py" &
    KINETIC_LENS_PID=$!
    echo "[run] kinetic lens viewer pid $KINETIC_LENS_PID  →  http://0.0.0.0:${CEREBEL_KINETIC_LENS_PORT}"
  fi
else
  echo "[run] warning: GEM-X python missing at $CEREBEL_GEMX_PYTHON" >&2
fi

"$ROOT/.venv/bin/python" -u "$ROOT/server.py" &
SERVER_PID=$!
echo "[run] capture server pid $SERVER_PID  →  ${CEREBEL_HOST}:${CEREBEL_PORT}"
echo "[run] attached — warm worker logs + GEM-X job progress print here. Ctrl+C to stop."
echo

wait -n "$SERVER_PID" ${WORKER_PID:+$WORKER_PID} ${VIEWER_PID:+$VIEWER_PID} ${KINETIC_LENS_PID:+$KINETIC_LENS_PID} 2>/dev/null || wait
