#!/usr/bin/env bash
# Start, stop or restart the consumer web app (motion_agent chat_server) in the background.
# Works on GPU containers without systemd. Settings come from .env.product next to this repo.
#
#   bash deploy/run-product.sh start|stop|restart|status
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${CEREBEL_PRODUCT_ENV:-$ROOT/.env.product}"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
LOG="$ROOT/product_server.log"
PIDFILE="$ROOT/data/product-server.pid"

start() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "missing $ENV_FILE (copy deploy/env.product.example and fill it in)" >&2
    exit 1
  fi
  set -a; source "$ENV_FILE"; set +a
  mkdir -p "$(dirname "$PIDFILE")"
  cd "$ROOT"
  nohup "$PY" -u -m motion_agent.app.chat_server >> "$LOG" 2>&1 < /dev/null &
  echo $! > "$PIDFILE"
  echo "started pid $(cat "$PIDFILE"), log $LOG"
}

stop() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" || true
  fi
  pkill -f "motion_agent.app.chat_server" 2>/dev/null || true
  sleep 1
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status)
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "running pid $(cat "$PIDFILE")"
    else
      echo "not running"; exit 1
    fi ;;
  *) echo "usage: $0 start|stop|restart|status" >&2; exit 2 ;;
esac
