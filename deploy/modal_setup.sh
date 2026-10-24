#!/usr/bin/env bash
# One-time Modal setup for the GEM-X GPU function. Run on the GPU server from /root/CerebelServer.
set -euo pipefail
cd "$(dirname "$0")/.."
GEMX_ROOT="${CEREBEL_GEMX_ROOT:-/root/GEM-X}"
PY="${PYTHON:-python3}"

echo "== 1/5 install the Modal client"
"$PY" -m pip install -q --upgrade modal

if ! "$PY" -m modal token info >/dev/null 2>&1; then
  echo "== 2/5 log in: open the link printed below in your browser and approve"
  "$PY" -m modal token new
else
  echo "== 2/5 already logged in to Modal"
fi

echo "== 3/5 upload model weights (~6.4 GB, once; re-running only uploads what is missing)"
"$PY" -m modal volume create cerebel-gemx-inputs >/dev/null 2>&1 || true
"$PY" -m modal volume create cerebel-gemx-cache >/dev/null 2>&1 || true
for d in checkpoints pretrained mhr_data soma_data soma_assets; do
  if [ -e "$GEMX_ROOT/inputs/$d" ]; then
    echo "   uploading inputs/$d"
    "$PY" -m modal volume put cerebel-gemx-inputs "$GEMX_ROOT/inputs/$d" "/$d" || echo "   (already uploaded, skipping)"
  fi
done

echo "== 4/5 build the image and deploy (first build takes 10-20 min)"
CEREBEL_GEMX_ROOT="$GEMX_ROOT" "$PY" -m modal deploy deploy/modal_gemx.py

echo "== 5/5 smoke test on a short clip"
SAMPLE="${1:-}"
if [ -z "$SAMPLE" ]; then
  SAMPLE=$(ls -t data/sessions/*/upload/source_video.* 2>/dev/null | head -1 || true)
fi
if [ -n "$SAMPLE" ]; then
  CEREBEL_GEMX_ROOT="$GEMX_ROOT" "$PY" -m modal run deploy/modal_gemx.py::smoke --video "$SAMPLE"
else
  echo "   no sample video found; run: modal run deploy/modal_gemx.py::smoke --video <clip.mp4>"
fi
echo
echo "Done. To use it, add CEREBEL_GPU_BACKEND=modal to .env.product and restart: bash deploy/run-product.sh restart"
