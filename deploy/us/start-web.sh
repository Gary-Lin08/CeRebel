#!/usr/bin/env bash
# systemd entry point for cerebel-web.service (US droplet).
# Loads the secrets file (.env.product, typed by the founder), then the machine layout
# written by setup-droplet.sh, then runs the web app in the foreground.
# With arguments it runs those python arguments in the same environment instead, e.g.
#   start-web.sh -m motion_agent.app.retention --dry-run      (cerebel-retention.service runs it daily)
# bash `source` is used instead of systemd EnvironmentFile because .env.product has
# inline comments and quoted values that EnvironmentFile would read literally.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="${CEREBEL_PRODUCT_ENV:-$APP_DIR/.env.product}"
LAYOUT_FILE="${CEREBEL_LAYOUT_ENV:-/etc/cerebel/droplet.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE: copy deploy/env.product.example and fill it in" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
# shellcheck disable=SC1090
[[ -f "$LAYOUT_FILE" ]] && source "$LAYOUT_FILE"
set +a
CONDA_ENV="${CEREBEL_CONDA_ENV:-/opt/miniforge/envs/cerebel}"   # droplet.env sets it

# Same effect as `conda activate`: the env's python, opensim libraries and tools come first.
export PATH="$CONDA_ENV/bin:$PATH"
export CONDA_PREFIX="$CONDA_ENV"
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "${CEREBEL_DATA_DIR:-$APP_DIR/data}"
cd "$APP_DIR"
if [[ $# -gt 0 ]]; then
  exec "$CONDA_ENV/bin/python" -u "$@"
fi
exec "$CONDA_ENV/bin/python" -u -m motion_agent.app.chat_server
