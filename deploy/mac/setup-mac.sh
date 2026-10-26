#!/usr/bin/env bash
# CeRebel web host on a Mac (the Mac mini in Boston). GPU inference runs on Modal; this Mac
# serves the web app on 127.0.0.1:8092 and does the CPU post-processing (SOMA decode, OpenSim
# IK, estimated dynamics, sport detection). The public address comes from a Cloudflare Tunnel
# (cloudflared), so no port is opened on the router. See deploy/mac/DEPLOY_MAC.md.
#
# Run as the normal macOS user that stays logged in (not with sudo):
#   bash deploy/mac/setup-mac.sh
# Safe to run again: every step checks what is already there.
#
# Knobs (environment): APP_DIR (default: this checkout), CEREBEL_HOME (default ~/cerebel),
# CONDA_DIR (default ~/miniforge3), CONDA_ENV (use an existing env instead), GEMX_COMMIT,
# NO_LAUNCHD=1 (install everything but do not load the launchd jobs).
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
CEREBEL_HOME="${CEREBEL_HOME:-$HOME/cerebel}"
CONDA_DIR="${CONDA_DIR:-$HOME/miniforge3}"
CONDA_ENV="${CONDA_ENV:-$CONDA_DIR/envs/cerebel}"
PY="$CONDA_ENV/bin/python"
GEMX_DIR="$CEREBEL_HOME/GEM-X"
GEMX_REPO="https://github.com/NVlabs/GEM-X.git"
OSIM_MODELS_DIR="$CEREBEL_HOME/opensim-models"
BACKUP_DIR="$CEREBEL_HOME/backups"
LOG_DIR="$CEREBEL_HOME/logs"
LAYOUT_FILE="$CEREBEL_HOME/mac.env"
AGENTS="$HOME/Library/LaunchAgents"
US="$APP_DIR/deploy/us"
MAC="$APP_DIR/deploy/mac"

log() { printf '\n== %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "this script is for macOS; on Linux use deploy/us/setup-droplet.sh"
[[ $EUID -ne 0 ]] || die "run as your normal user, not with sudo"
[[ -f "$APP_DIR/motion_agent/app/chat_server.py" ]] || die "APP_DIR=$APP_DIR is not a CeRebel checkout"

# ---------------------------------------------------------------- 1. Homebrew tools
log "1/8 Homebrew tools (ffmpeg, git-lfs, cloudflared)"
if [[ "${SKIP_BREW:-0}" == "1" ]]; then
  # Test installs: ffmpeg and git-lfs must already be on PATH.
  BREW_BIN="$(dirname "$(command -v git-lfs || die "SKIP_BREW=1 needs git-lfs on PATH")")"
  command -v ffmpeg >/dev/null || die "SKIP_BREW=1 needs ffmpeg on PATH"
else
  BREW="$(command -v brew || true)"
  [[ -z "$BREW" && -x /opt/homebrew/bin/brew ]] && BREW=/opt/homebrew/bin/brew
  [[ -z "$BREW" && -x /usr/local/bin/brew ]] && BREW=/usr/local/bin/brew
  [[ -n "$BREW" ]] || die "Homebrew is missing. Install it from https://brew.sh (it asks for your Mac password), then run this script again."
  for pkg in ffmpeg git-lfs cloudflared; do
    "$BREW" list --formula "$pkg" >/dev/null 2>&1 || "$BREW" install -q "$pkg"
  done
  BREW_BIN="$("$BREW" --prefix)/bin"
  "$BREW_BIN/git-lfs" install --skip-repo >/dev/null
fi

# ---------------------------------------------------------------- 2. Python env
log "2/8 conda env at $CONDA_ENV (python 3.10 + opensim 4.5.2)"
if [[ ! -x "$PY" ]]; then
  if [[ ! -x "$CONDA_DIR/bin/conda" ]]; then
    curl -fsSL -o /tmp/miniforge.sh \
      "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-MacOSX-$(uname -m).sh"
    bash /tmp/miniforge.sh -b -p "$CONDA_DIR"
    rm -f /tmp/miniforge.sh
  fi
  "$CONDA_DIR/bin/conda" create -y -q -p "$CONDA_ENV" \
    -c opensim-org -c conda-forge --override-channels \
    python=3.10 numpy=1.25 opensim=4.5.2
fi
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$US/requirements-web.txt"

# ---------------------------------------------------------------- 3. GEM-X SOMA layer
log "3/8 GEM-X SOMA body layer at $GEMX_DIR (code + SOMA-X assets only; no GPU weights)"
mkdir -p "$CEREBEL_HOME"
GEMX_COMMIT="${GEMX_COMMIT:-}"
if [[ -z "$GEMX_COMMIT" && -f "$APP_DIR/deploy/gemx_overlay/BASE_COMMIT" ]]; then
  GEMX_COMMIT="$(tr -d '[:space:]' < "$APP_DIR/deploy/gemx_overlay/BASE_COMMIT")"
fi
GEMX_COMMIT="${GEMX_COMMIT:-32992550dba114c62243fb55e361311972dce8f9}"
if [[ ! -d "$GEMX_DIR/.git" ]]; then
  GIT_LFS_SKIP_SMUDGE=1 git clone -q "$GEMX_REPO" "$GEMX_DIR"
fi
if [[ "$(git -C "$GEMX_DIR" rev-parse HEAD)" != "$GEMX_COMMIT" ]]; then
  git -C "$GEMX_DIR" fetch -q origin
  git -C "$GEMX_DIR" checkout -q "$GEMX_COMMIT"
fi
GIT_LFS_SKIP_SMUDGE=1 git -C "$GEMX_DIR" submodule update -q --init third_party/soma
PATH="$BREW_BIN:$PATH" git -C "$GEMX_DIR/third_party/soma" lfs pull
"$PY" -m pip install -q -e "$GEMX_DIR/third_party/soma"
mkdir -p "$GEMX_DIR/inputs"
ln -sfn "$GEMX_DIR/third_party/soma/assets" "$GEMX_DIR/inputs/soma_assets"

# ---------------------------------------------------------------- 4. OpenSim geometry
log "4/8 OpenSim mesh geometry (opensim-models/Geometry) for the bones GLB export"
if [[ ! -d "$OSIM_MODELS_DIR/Geometry" ]]; then
  rm -rf "$OSIM_MODELS_DIR"
  git clone -q --depth 1 --filter=blob:none --sparse \
    https://github.com/opensim-org/opensim-models.git "$OSIM_MODELS_DIR"
  git -C "$OSIM_MODELS_DIR" sparse-checkout set Geometry
fi
# kinetic-lens/analysis/Geometry points at /tmp/opensim-models/Geometry; macOS empties /tmp at
# boot, so start-mac.sh recreates this link every time the web app starts.
ln -sfn "$OSIM_MODELS_DIR" /tmp/opensim-models

# ---------------------------------------------------------------- 5. data + settings
log "5/8 data dir and settings"
for d in data data/product data/sessions data/gemx_outputs; do
  mkdir -p "$APP_DIR/$d"
done
chmod 750 "$APP_DIR/data"
mkdir -p "$BACKUP_DIR" "$LOG_DIR"
chmod 700 "$BACKUP_DIR"
cat > "$LAYOUT_FILE" <<ENVF
# Machine layout written by deploy/mac/setup-mac.sh. Loaded after .env.product, so these win.
CEREBEL_DATA_DIR=$APP_DIR/data
CEREBEL_SESSIONS_DIR=$APP_DIR/data/sessions
CEREBEL_GEMX_OUTPUT_ROOT=$APP_DIR/data/gemx_outputs
CEREBEL_GEMX_ROOT=$GEMX_DIR
CEREBEL_SOMA_ASSETS=$GEMX_DIR/third_party/soma/assets
CEREBEL_GEMX_PYTHON=$PY
CEREBEL_IK_PYTHON=$PY
CEREBEL_CONDA_ENV=$CONDA_ENV
CEREBEL_KINETIC_LENS_ROOT=$APP_DIR/kinetic-lens
CEREBEL_IK_DEVICE=cpu
CEREBEL_GEMX_DEVICE=cpu
MOTION_AGENT_SMPLX_DEVICE=cpu
CEREBEL_FAKE_EGOCENTRIC=0
CEREBEL_GPU_BACKEND=modal
# Not a provider the Privacy Policy names yet: privacy_guard reports it until consent.PROCESSORS
# lists this machine and VIDEO_CONSENT_VERSION is bumped.
CEREBEL_HOST_PROVIDER=mac-mini-boston
MOTION_AGENT_HOST=127.0.0.1
MOTION_AGENT_PORT=8092
MPLBACKEND=Agg
PYTHONUNBUFFERED=1
OMP_NUM_THREADS=2
OPENBLAS_NUM_THREADS=2
PATH=$CONDA_ENV/bin:$BREW_BIN:/usr/bin:/bin:/usr/sbin:/sbin
ENVF
chmod 644 "$LAYOUT_FILE"

ENV_FILE="$APP_DIR/.env.product"
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$APP_DIR/deploy/env.product.example" "$ENV_FILE"
  # Cloudflare's free plan rejects request bodies over 100 MB, so uploads stop at 95 MB.
  sed -i '' \
    -e "s#/root/CerebelServer#$APP_DIR#g" \
    -e "s#^CEREBEL_PUBLIC_URL=[^ ]*#CEREBEL_PUBLIC_URL=https://playground.cerebel.tech#" \
    -e "s#^CEREBEL_GPU_BACKEND=[^ ]*#CEREBEL_GPU_BACKEND=modal#" \
    -e "s#^CEREBEL_QUEUE_WORKERS=[^ ]*#CEREBEL_QUEUE_WORKERS=2#" \
    -e "s|^CEREBEL_UPLOAD_MAX_BYTES=.*|CEREBEL_UPLOAD_MAX_BYTES=99614720             # 95 MB: Cloudflare free plan limit is 100 MB|" \
    -e "s#^MOTION_AGENT_BASE_URL=[^ ]*#MOTION_AGENT_BASE_URL=https://api.together.xyz/v1#" \
    "$ENV_FILE"
  echo "created $ENV_FILE from the template: fill in the secrets (see DEPLOY_MAC.md step 4)"
fi
chmod 600 "$ENV_FILE"
bash -n "$ENV_FILE" 2>/dev/null || warn "$ENV_FILE has a shell syntax error (a value with spaces or < > needs 'quotes')"

# ---------------------------------------------------------------- 6. launchd jobs
log "6/8 launchd jobs (web app always on; daily retention 03:41 and backup 04:17 local time)"
mkdir -p "$AGENTS"
render() {  # render <template> <dest>
  sed -e "s#@APP_DIR@#$APP_DIR#g" -e "s#@LAYOUT_FILE@#$LAYOUT_FILE#g" \
      -e "s#@LOG_DIR@#$LOG_DIR#g" -e "s#@BACKUP_DIR@#$BACKUP_DIR#g" \
      -e "s#@OSIM_MODELS_DIR@#$OSIM_MODELS_DIR#g" "$1" > "$2"
  plutil -lint -s "$2"
}
for job in tech.cerebel.web tech.cerebel.retention tech.cerebel.backup; do
  render "$MAC/$job.plist" "$AGENTS/$job.plist"
  if [[ "${NO_LAUNCHD:-0}" != "1" ]]; then
    launchctl bootout "gui/$UID/$job" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$AGENTS/$job.plist"
  fi
done

# ---------------------------------------------------------------- 7. self-check
log "7/8 self-check"
fail=0
CEREBEL_LAYOUT_ENV="$LAYOUT_FILE" CEREBEL_CONDA_ENV="$CONDA_ENV" \
  bash "$US/start-web.sh" - <<PYCHK || fail=1
import importlib, sys
mods = ["opensim", "torch", "numpy", "scipy", "matplotlib", "openai", "modal", "warp", "soma",
        "motion_agent.app.chat_server", "motion_agent.app.product_api", "upload_server",
        "gemx_pipeline", "ik_pipeline", "cerebel_ik_retarget"]
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as exc:
        bad.append(f"{m}: {exc!r}")
sys.path.insert(0, "$GEMX_DIR")
try:
    from gem.utils.soma_utils.soma_layer import SomaLayer  # noqa: F401
except Exception as exc:
    bad.append(f"gem SomaLayer: {exc!r}")
print("python imports:", "ok" if not bad else "FAILED")
for b in bad:
    print("  ", b)
sys.exit(1 if bad else 0)
PYCHK
for f in "$APP_DIR/kinetic-lens/retargeting_neck_v2/models/RajagopalLaiUhlrich2023_neck.osim" \
         "$APP_DIR/kinetic-lens/analysis/Geometry/r_pelvis.vtp" \
         "$GEMX_DIR/third_party/soma/assets/MHR/mhr_model_lod6.pt" \
         "$GEMX_DIR/third_party/soma/assets/SOMA_neutral.npz"; do
  if [[ -s "$f" ]]; then echo "file ok: $f"; else echo "MISSING: $f"; fail=1; fi
done
if [[ "${NO_LAUNCHD:-0}" != "1" ]]; then
  up=0
  for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null http://127.0.0.1:8092/terms; then up=1; break; fi
    sleep 1
  done
  if [[ $up -eq 1 ]]; then echo "web app answers on 127.0.0.1:8092"; else
    echo "web app is not answering yet: see $LOG_DIR/web.log"; fail=1; fi
fi

# ---------------------------------------------------------------- 8. next steps
log "8/8 next steps"
grep -qE '^STRIPE_SECRET_KEY=.+' "$ENV_FILE" || echo "- fill in $ENV_FILE (DEPLOY_MAC.md step 4), then: launchctl kickstart -k gui/$UID/tech.cerebel.web"
pmset -g | grep -qE '^ *sleep +0' || echo "- stop the Mac from sleeping: sudo pmset -a sleep 0 disksleep 0 autorestart 1 (DEPLOY_MAC.md step 2)"
pgrep -x cloudflared >/dev/null || echo "- connect the Cloudflare Tunnel (DEPLOY_MAC.md step 5)"
[[ $fail -eq 0 ]] && echo "self-check passed" || { echo "self-check found problems (above)"; exit 1; }
