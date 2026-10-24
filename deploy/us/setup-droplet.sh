#!/usr/bin/env bash
# CeRebel web box on a fresh Ubuntu 24.04 droplet (DigitalOcean NYC, 4 vCPU / 8 GB).
# GPU inference runs on Modal; this box serves the web app and does the CPU post-processing
# (SOMA decode, OpenSim IK, estimated dynamics, sport detection).
#
# Run as root. Safe to run again: every step checks what is already there.
#
#   Code via rsync (recommended for the first install, from the Mac):
#     rsync -az --exclude .git --exclude data/ --exclude '.env*' --exclude '__pycache__' \
#       --exclude .venv --exclude .venv-ik --exclude kinetic-lens/node_modules \
#       --exclude kinetic-lens/retargeting_neck_v2/outputs --exclude .DS_Store --exclude '*.log' \
#       ~/Developer/CeRebel/ root@<droplet-ip>:/opt/cerebel/
#     ssh root@<droplet-ip> bash /opt/cerebel/deploy/us/setup-droplet.sh
#
#   Code via git clone with a read-only GitHub deploy key:
#     scp deploy/us/setup-droplet.sh root@<droplet-ip>:   (or paste it into nano)
#     ssh root@<droplet-ip> CODE_SOURCE=git bash setup-droplet.sh
#     (first run prints a public key: add it on GitHub > repo > Settings > Deploy keys,
#      read-only, then run the same command again; later runs with CODE_SOURCE=git pull)
#     Everything this kit needs (deploy/us/, motion_agent/app/retention.py, consent.py)
#     must be committed and pushed first; rsync also copies uncommitted files.
#
#   Updates after the first install: either GitHub Actions (.github/workflows/deploy.yml,
#   rsync as user cerebel) or `git pull` (CODE_SOURCE=git), not both: rsync'd changes make
#   the next pull refuse to run. Re-run this script after requirements-web.txt changes.
#
#   What it sets up: user cerebel; ffmpeg, git, Caddy, Miniforge; conda env (Python 3.10,
#   opensim 4.5.2, deploy/us/requirements-web.txt); GEM-X SOMA layer + OpenSim meshes;
#   cerebel-web.service; Caddy for $DOMAIN (auto TLS, 200 MB bodies, security headers);
#   ufw 22/80/443; unattended-upgrades; daily retention + backup timers (14-day backups).
#
# Knobs (environment): DOMAIN, CODE_SOURCE (auto|git|rsync), REPO_URL, APP_DIR, CONDA_DIR,
# GEMX_DIR, GEMX_COMMIT, SWAP_GB, ACME_EMAIL.
set -euo pipefail

DOMAIN="${DOMAIN:-playground.cerebel.tech}"
CODE_SOURCE="${CODE_SOURCE:-auto}"
REPO_URL="${REPO_URL:-git@github.com:Gary-Lin08/CeRebel.git}"
APP_USER=cerebel
APP_DIR="${APP_DIR:-/opt/cerebel}"
CONDA_DIR="${CONDA_DIR:-/opt/miniforge}"
ENV_DIR="$CONDA_DIR/envs/cerebel"
PY="$ENV_DIR/bin/python"
GEMX_DIR="${GEMX_DIR:-/opt/GEM-X}"
GEMX_REPO="https://github.com/NVlabs/GEM-X.git"
OSIM_MODELS_DIR=/opt/opensim-models
SWAP_GB="${SWAP_GB:-4}"
ACME_EMAIL="${ACME_EMAIL:-}"
BACKUP_DIR=/var/backups/cerebel

log() { printf '\n== %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
as_app() { sudo -u "$APP_USER" -H "$@"; }

[[ $EUID -eq 0 ]] || die "run as root (sudo bash $0)"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${VERSION_ID:-}" == "24.04" ]] || warn "written for Ubuntu 24.04, this is ${PRETTY_NAME:-unknown}"
fi
export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------- 1. packages
log "1/12 system packages"
apt-get update -q
apt-get install -y -q --no-install-recommends \
  ca-certificates curl gnupg debian-keyring debian-archive-keyring apt-transport-https \
  git git-lfs ffmpeg rsync ufw unattended-upgrades sudo cron tar gzip \
  libgl1 libegl1 libglib2.0-0 libgomp1
git lfs install --system --skip-repo >/dev/null

if ! command -v caddy >/dev/null 2>&1; then
  log "installing Caddy from its official apt repository"
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi

# ---------------------------------------------------------------- 2. swap
log "2/12 swap (${SWAP_GB} GB) so a burst of OpenSim jobs cannot OOM-kill the web app"
if [[ "$SWAP_GB" -gt 0 ]] && ! swapon --show=NAME --noheadings | grep -q .; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l "${SWAP_GB}G" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
  fi
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---------------------------------------------------------------- 3. user
log "3/12 user '$APP_USER'"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --create-home --home-dir "/home/$APP_USER" --shell /bin/bash "$APP_USER"
fi
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "/home/$APP_USER/.ssh"
touch "/home/$APP_USER/.ssh/authorized_keys"
chown "$APP_USER:$APP_USER" "/home/$APP_USER/.ssh/authorized_keys"
chmod 600 "/home/$APP_USER/.ssh/authorized_keys"
# GitHub Actions logs in as this user to deploy; it may restart the app and nothing else.
cat > /etc/sudoers.d/cerebel-deploy <<SUDO
$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart cerebel-web, /usr/bin/systemctl restart cerebel-web.service, /usr/bin/systemctl status cerebel-web, /usr/bin/systemctl status cerebel-web.service, /usr/bin/systemctl is-active cerebel-web
SUDO
chmod 440 /etc/sudoers.d/cerebel-deploy
visudo -cf /etc/sudoers.d/cerebel-deploy >/dev/null || die "sudoers rule did not validate"

# ---------------------------------------------------------------- 4. code
log "4/12 code at $APP_DIR"
install -d -m 755 "$APP_DIR"
if [[ -f "$APP_DIR/motion_agent/app/chat_server.py" ]]; then
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
  if [[ -d "$APP_DIR/.git" && "$CODE_SOURCE" == "git" ]]; then
    as_app git -C "$APP_DIR" pull --ff-only || warn "git pull failed; keeping the current code"
  fi
  echo "code present ($([[ -d $APP_DIR/.git ]] && echo git checkout || echo rsync copy))"
else
  [[ "$CODE_SOURCE" == "rsync" ]] && die "no code in $APP_DIR yet: rsync it from the Mac (see the top of this script), then run again"
  KEY="/home/$APP_USER/.ssh/github_deploy"
  if [[ ! -f "$KEY" ]]; then
    as_app ssh-keygen -q -t ed25519 -N '' -C "cerebel-droplet-deploy" -f "$KEY"
  fi
  cat > "/home/$APP_USER/.ssh/config" <<SSHCFG
Host github.com
  IdentityFile $KEY
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
SSHCFG
  chown "$APP_USER:$APP_USER" "/home/$APP_USER/.ssh/config"
  chmod 600 "/home/$APP_USER/.ssh/config"
  if as_app git ls-remote "$REPO_URL" HEAD >/dev/null 2>&1; then
    # Clone into a temp dir then move, because $APP_DIR may already hold an empty data/ dir.
    tmp="$(mktemp -d /opt/.cerebel-clone.XXXX)"
    chown "$APP_USER:$APP_USER" "$tmp"
    as_app git clone --depth 50 "$REPO_URL" "$tmp/src"
    shopt -s dotglob
    mv "$tmp/src"/* "$APP_DIR"/
    shopt -u dotglob
    rm -rf "$tmp"
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
  else
    echo
    echo "GitHub does not accept this server's key yet. Add this as a READ-ONLY deploy key:"
    echo "  GitHub > Gary-Lin08/CeRebel > Settings > Deploy keys > Add deploy key"
    echo "  (leave 'Allow write access' unticked)"
    echo
    cat "$KEY.pub"
    echo
    echo "Then run this script again. Or skip git and rsync the code from the Mac instead."
    exit 2
  fi
fi
US="$APP_DIR/deploy/us"
[[ -f "$US/requirements-web.txt" ]] || die "$US is missing: the code copy is incomplete"

# ---------------------------------------------------------------- 5. Miniforge + env
log "5/12 Miniforge at $CONDA_DIR and env 'cerebel' (python 3.10 + opensim)"
if [[ ! -x "$CONDA_DIR/bin/conda" ]]; then
  arch="$(uname -m)"
  curl -fsSL -o /tmp/miniforge.sh \
    "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${arch}.sh"
  bash /tmp/miniforge.sh -b -p "$CONDA_DIR"
  rm -f /tmp/miniforge.sh
fi
if [[ ! -x "$PY" ]] || ! "$PY" -c 'import opensim' >/dev/null 2>&1; then
  "$CONDA_DIR/bin/conda" create -y -q -p "$ENV_DIR" \
    -c opensim-org -c conda-forge --override-channels \
    python=3.10 numpy=1.25 opensim=4.5.2
fi
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$US/requirements-web.txt"

# ---------------------------------------------------------------- 6. GEM-X SOMA layer (CPU)
log "6/12 GEM-X SOMA body layer at $GEMX_DIR (code + SOMA-X assets only; no GPU weights)"
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
git -C "$GEMX_DIR/third_party/soma" lfs pull
"$PY" -m pip install -q -e "$GEMX_DIR/third_party/soma"
install -d "$GEMX_DIR/inputs"
ln -sfn "$GEMX_DIR/third_party/soma/assets" "$GEMX_DIR/inputs/soma_assets"

# ---------------------------------------------------------------- 7. OpenSim geometry
log "7/12 OpenSim mesh geometry (opensim-models/Geometry) for the bones GLB export"
if [[ ! -d "$OSIM_MODELS_DIR/Geometry" ]]; then
  rm -rf "$OSIM_MODELS_DIR"
  git clone -q --depth 1 --filter=blob:none --sparse \
    https://github.com/opensim-org/opensim-models.git "$OSIM_MODELS_DIR"
  git -C "$OSIM_MODELS_DIR" sparse-checkout set Geometry
fi
# The repo's kinetic-lens/analysis/Geometry is a symlink to /tmp/opensim-models/Geometry.
# Recreate that link at every boot (systemd-tmpfiles), since /tmp is cleared on reboot.
echo "L+ /tmp/opensim-models - - - - $OSIM_MODELS_DIR" > /etc/tmpfiles.d/cerebel-opensim.conf
systemd-tmpfiles --create /etc/tmpfiles.d/cerebel-opensim.conf

# ---------------------------------------------------------------- 8. data + config
log "8/12 data dir and settings"
for d in data data/product data/sessions data/gemx_outputs; do
  install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/$d"
done
install -d -m 755 /etc/cerebel
cat > /etc/cerebel/droplet.env <<ENVF
# Machine layout written by setup-droplet.sh. Loaded after .env.product, so these win.
CEREBEL_DATA_DIR=$APP_DIR/data
CEREBEL_SESSIONS_DIR=$APP_DIR/data/sessions
CEREBEL_GEMX_OUTPUT_ROOT=$APP_DIR/data/gemx_outputs
CEREBEL_GEMX_ROOT=$GEMX_DIR
CEREBEL_SOMA_ASSETS=$GEMX_DIR/third_party/soma/assets
CEREBEL_GEMX_PYTHON=$PY
CEREBEL_IK_PYTHON=$PY
CEREBEL_CONDA_ENV=$ENV_DIR
CEREBEL_KINETIC_LENS_ROOT=$APP_DIR/kinetic-lens
CEREBEL_IK_DEVICE=cpu
CEREBEL_GEMX_DEVICE=cpu
MOTION_AGENT_SMPLX_DEVICE=cpu
CEREBEL_FAKE_EGOCENTRIC=0
MOTION_AGENT_HOST=127.0.0.1
MOTION_AGENT_PORT=8092
MPLBACKEND=Agg
PYTHONUNBUFFERED=1
# Up to CEREBEL_QUEUE_WORKERS jobs post-process at once on 4 vCPUs: keep each one from
# grabbing every core (torch / OpenBLAS threads).
OMP_NUM_THREADS=2
OPENBLAS_NUM_THREADS=2
ENVF
chmod 644 /etc/cerebel/droplet.env

ENV_FILE="$APP_DIR/.env.product"
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$APP_DIR/deploy/env.product.example" "$ENV_FILE"
  sed -i \
    -e "s#/root/CerebelServer#$APP_DIR#g" \
    -e "s#^CEREBEL_PUBLIC_URL=[^ ]*#CEREBEL_PUBLIC_URL=https://$DOMAIN#" \
    -e "s#^CEREBEL_GPU_BACKEND=[^ ]*#CEREBEL_GPU_BACKEND=modal#" \
    -e "s#^CEREBEL_QUEUE_WORKERS=[^ ]*#CEREBEL_QUEUE_WORKERS=4#" \
    -e "s|^CEREBEL_UPLOAD_MAX_BYTES=.*|CEREBEL_UPLOAD_MAX_BYTES=209715200            # 200 MB, same as the Caddy limit|" \
    -e "s#^MOTION_AGENT_BASE_URL=[^ ]*#MOTION_AGENT_BASE_URL=https://api.together.xyz/v1#" \
    -e "s#^CEREBEL_MAIL_FROM=CeRebel <\(.*\)>\$#CEREBEL_MAIL_FROM='CeRebel <\1>'#" \
    "$ENV_FILE"
  if ! grep -q '^MODAL_TOKEN_ID=' "$ENV_FILE"; then
    cat >> "$ENV_FILE" <<'MODAL'

# --- Modal (GPU) credentials for this server: modal.com > Settings > API Tokens > New token
MODAL_TOKEN_ID=
MODAL_TOKEN_SECRET=
MODAL
  fi
  echo "created $ENV_FILE from the template: fill in the secrets (see DEPLOY_US.md)"
fi
chown "$APP_USER:$APP_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"
bash -n "$ENV_FILE" 2>/dev/null || warn "$ENV_FILE has a shell syntax error (a value with spaces or < > needs 'quotes')"

# ---------------------------------------------------------------- 9. systemd
log "9/12 systemd units (web app, daily retention, daily backup)"
install -m 755 "$US/cerebel-backup.sh" /usr/local/sbin/cerebel-backup
for unit in cerebel-web.service cerebel-retention.service cerebel-retention.timer \
            cerebel-backup.service cerebel-backup.timer; do
  sed -e "s#/opt/cerebel#$APP_DIR#g" -e "s#/var/backups/cerebel#$BACKUP_DIR#g" \
    "$US/$unit" > "/etc/systemd/system/$unit"
  chmod 644 "/etc/systemd/system/$unit"
done
# Earlier versions of this script used cron for these jobs.
rm -f /etc/cron.d/cerebel-backup /etc/cron.d/cerebel-retention
systemctl daemon-reload
systemctl enable cerebel-web.service >/dev/null
systemctl restart cerebel-web.service

# ---------------------------------------------------------------- 10. Caddy
log "10/12 Caddy reverse proxy for $DOMAIN (automatic HTTPS)"
install -d -o caddy -g caddy /var/log/caddy
sed -e "s#{\$CEREBEL_DOMAIN}#$DOMAIN#g" "$US/Caddyfile" > /etc/caddy/Caddyfile.new
if [[ -n "$ACME_EMAIL" ]]; then
  sed -i "s#^\t\# email you@example.com#\temail $ACME_EMAIL#" /etc/caddy/Caddyfile.new
fi
caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile.new >/dev/null
mv /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile
systemctl enable caddy >/dev/null
systemctl reload caddy 2>/dev/null || systemctl restart caddy

# ---------------------------------------------------------------- 11. firewall, updates, backups
log "11/12 firewall (22/80/443), unattended upgrades, daily retention + backup timers"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443 >/dev/null          # tcp + udp (HTTP/3)
ufw --force enable >/dev/null

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'APT'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
APT
cat > /etc/apt/apt.conf.d/52cerebel-unattended <<'APT'
// Security updates install daily. A kernel update reboots the box at 09:00 UTC
// (4-5 am New York); cerebel-web and caddy start again by themselves.
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "09:00";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
APT
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

# Backups: 14 daily copies of data/product (the privacy policy promises 14 days).
# Retention: deletes expired videos and inactive accounts every day (the policy promises it).
# Both are systemd timers (Persistent=true: a run missed while the box was off happens at boot).
# Logs: journalctl -u cerebel-backup / journalctl -u cerebel-retention
install -d -m 700 "$BACKUP_DIR"
[[ -f "$APP_DIR/motion_agent/app/retention.py" ]] || die "motion_agent/app/retention.py is missing: the code copy is too old"
systemctl enable --now cerebel-retention.timer cerebel-backup.timer >/dev/null

# ---------------------------------------------------------------- 12. self-check
log "12/12 self-check"
fail=0
as_app bash "$US/start-web.sh" - <<PYCHK || fail=1
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
up=0
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null http://127.0.0.1:8092/terms; then up=1; break; fi
  sleep 1
done
if [[ $up -eq 1 ]]; then
  echo "web app answers on 127.0.0.1:8092"
else
  echo "web app is not answering yet: journalctl -u cerebel-web -n 50"; fail=1
fi
if as_app bash "$US/start-web.sh" -m motion_agent.app.retention --dry-run >/dev/null 2>&1; then
  echo "retention job runs (dry run ok); timers:"
  systemctl list-timers --no-pager cerebel-retention.timer cerebel-backup.timer | sed -n '1,3p'
else
  echo "retention dry run FAILED: sudo -u $APP_USER bash $US/start-web.sh -m motion_agent.app.retention --dry-run"; fail=1
fi

echo
echo "Done. Next steps (DEPLOY_US.md):"
echo "  1. nano $ENV_FILE        # type the keys yourself"
echo "  2. systemctl restart cerebel-web"
echo "  3. point DNS for $DOMAIN to this server, then open https://$DOMAIN"
[[ $fail -eq 0 ]] || { echo "Some checks failed (see above)."; exit 1; }
