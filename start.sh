#!/usr/bin/env bash
# Bootstrap deps, then stay in the foreground (live logs + GEM-X progress).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
VENV="$ROOT/.venv"
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"

if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON" -m venv "$VENV"
fi

"$VENV/bin/pip" install --upgrade pip -i "$PIP_INDEX"
"$VENV/bin/pip" install -r "$ROOT/requirements.txt" -i "$PIP_INDEX"

"$VENV/bin/python" -m grpc_tools.protoc \
  -I "$ROOT" \
  --python_out="$ROOT" \
  --grpc_python_out="$ROOT" \
  "$ROOT/capture.proto"

exec "$ROOT/run.sh" "$@"
