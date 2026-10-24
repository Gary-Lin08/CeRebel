#!/usr/bin/env bash
# Update the Mac host to the latest main and restart the web app (run on the Mac mini).
#   bash deploy/mac/update-mac.sh
# Re-run setup-mac.sh instead when deploy/us/requirements-web.txt or deploy/mac/ changed.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$APP_DIR"
git pull --ff-only origin main
launchctl kickstart -k "gui/$UID/tech.cerebel.web"
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null http://127.0.0.1:8092/terms; then echo "updated to $(git rev-parse --short HEAD), web app is up"; exit 0; fi
  sleep 1
done
echo "web app did not come back: tail -50 ~/cerebel/logs/web.log" >&2
exit 1
