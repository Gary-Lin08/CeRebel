#!/usr/bin/env bash
# One-time Modal setup for the GEM-X GPU function. Run from any machine that can reach
# modal.com (NOT the China GPU server). Builds in Modal's cloud; downloads weights from
# Hugging Face into a Modal Volume; nothing large is uploaded from this machine.
#   bash deploy/modal_setup.sh [smoke_clip.mp4]
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
SMOKE_GPUS="${CEREBEL_MODAL_SMOKE_GPUS:-L40S A100-40GB}"

echo "== 1/5 install the Modal client"
"$PY" -m pip install -q --upgrade pip modal

if ! "$PY" -m modal app list >/dev/null 2>&1; then
  echo "== 2/5 log in: approve the link printed below in your browser"
  "$PY" -m modal token new
else
  echo "== 2/5 already logged in to Modal"
fi

echo "== 3/5 build the image and deploy (first build takes 15-30 min)"
"$PY" -m modal deploy deploy/modal_gemx.py

echo "== 4/5 download model weights from Hugging Face into the Modal Volume (once)"
"$PY" -m modal run deploy/modal_gemx.py::prefetch

echo "== 5/5 smoke test (cold + warm) per GPU type"
SAMPLE="${1:-kinetic-lens/public/assets/layup-original.mp4}"
for g in $SMOKE_GPUS; do
  "$PY" -m modal run deploy/modal_gemx.py::smoke --video "$SAMPLE" --gpu "$g" || echo "   smoke on $g failed"
done
echo
echo "Done. On the web server set CEREBEL_GPU_BACKEND=modal plus MODAL_TOKEN_ID/MODAL_TOKEN_SECRET, then restart."
