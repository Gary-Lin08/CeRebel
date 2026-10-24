#!/bin/bash
# Double-click on the Mac: pulls the repo, installs the Modal CLI in its own venv,
# logs in to Modal (approve in the browser), deploys GEM-X, fetches weights, smoke-tests.
cd "$HOME/Developer/CeRebel" || exit 1
LOG="$PWD/deploy/.modal_setup.log"
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1
echo "== $(date) CeRebel Modal setup"
echo "== git pull"
git pull --ff-only origin main || echo "   (git pull failed; continuing with the local copy)"
if [ ! -f deploy/gemx_overlay/gemx-local-changes.patch ]; then
  echo "ERROR: deploy/gemx_overlay missing (git pull did not bring commit c8d943e)"; echo "EXIT=1"; exit 1
fi
VENV="$HOME/.cerebel-modal-venv"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
PYTHON="$VENV/bin/python" bash deploy/modal_setup.sh "$@"
echo "EXIT=$?"
echo "You can close this window."
