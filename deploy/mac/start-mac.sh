#!/usr/bin/env bash
# launchd entry point on the Mac host (tech.cerebel.web and tech.cerebel.retention).
# macOS empties /tmp at boot: recreate the bone-mesh link, then run the shared start script.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
[[ -n "${CEREBEL_OSIM_MODELS:-}" ]] && ln -sfn "$CEREBEL_OSIM_MODELS" /tmp/opensim-models
exec /bin/bash "$APP_DIR/deploy/us/start-web.sh" "$@"
