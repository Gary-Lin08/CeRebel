#!/usr/bin/env bash
# Run estimated inverse dynamics + CerebelPreview packaging for CeRebel sessions.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PREVIEW_ROOT="${CEREBEL_PREVIEW_ROOT:-/root/CerebelPreview}"
IK_PYTHON="${CEREBEL_IK_PYTHON:-$ROOT/.venv-ik/bin/python}"
MESH_PYTHON="${CEREBEL_MESH_PYTHON:-/root/GEM-X/.venv/bin/python}"
DATA_DIR="${CEREBEL_DATA_DIR:-$ROOT/data/sessions}"
DYNAMICS_SCRIPT="$ROOT/kinetic-lens/scripts/run_estimated_dynamics.py"

declare -A SESSIONS=(
  ["991B507E-AF0B-4DED-A4EC-9EE892230F0"]="/root/991b507eaf0be8deda4ec9ee892230f0_raw.mp4"
  ["FC4FE648-7D42-4560-832B-E2FE1B61B07A"]="/root/Golf.mov"
  ["69C0AF13-9ECF-4298-9914-DCF5A8B7BE39"]="/root/Jog.mov"
  ["BB6037F3-37EC-4876-ACEE-282061ECB537"]="/root/Shohei.mov"
)

run_dynamics() {
  local session_id="$1"
  local session_dir="$DATA_DIR/$session_id"
  local ik_dir="$session_dir/ik"
  local work="$ik_dir/dynamics-work"
  local markers="$ik_dir/opensim-work/KineticLens_markers.osim"
  local mot="$ik_dir/opensim-work/cerebel-ik.mot"
  local motion_json="$ik_dir/opensim-motion.json"
  local output="$ik_dir/estimated-dynamics.json"

  if [[ ! -f "$markers" || ! -f "$mot" || ! -f "$motion_json" ]]; then
    echo "[dynamics] skip $session_id — missing IK outputs" >&2
    return 1
  fi

  echo "[dynamics] $session_id"
  mkdir -p "$work"
  local -a cmd=(
    "$IK_PYTHON" "$DYNAMICS_SCRIPT"
    --model "$markers"
    --motion "$mot"
    --motion-json "$motion_json"
    --analysis-dir "$work"
    --output "$output"
  )
  if [[ -f "$ik_dir/motion-data.json" ]]; then
    cmd+=(--events-json "$ik_dir/motion-data.json")
  fi
  "${cmd[@]}"

  "$MESH_PYTHON" - <<PY
import json
from pathlib import Path
session = Path("$session_dir")
ik_json = session / "ik.json"
manifest = json.loads(ik_json.read_text(encoding="utf-8")) if ik_json.is_file() else {}
manifest["estimated_dynamics"] = "ik/estimated-dynamics.json"
ik_json.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(f"[dynamics] updated {ik_json}")
PY
}

package_session() {
  local session_id="$1"
  local session_dir="$DATA_DIR/$session_id"
  local fps
  fps="$("$MESH_PYTHON" -c "import json; print(json.load(open('$session_dir/header.json')).get('target_fps', 24))")"
  local hpe
  hpe="$("$MESH_PYTHON" -c "import json; print(json.load(open('$session_dir/gemx.json'))['hpe_results'])")"

  echo "[package] importing $session_id (fps=$fps)"
  "$PREVIEW_ROOT/import_session.sh" "$session_id" \
    --from-dir "$session_dir" \
    --hpe "$hpe" \
    --fps "$fps"

  local dest="$PREVIEW_ROOT/data/sessions/$session_id"
  echo "[package] baking mesh for $session_id"
  "$MESH_PYTHON" "$PREVIEW_ROOT/export_mesh.py" "$dest" --device "${CEREBEL_PREVIEW_DEVICE:-cuda:0}"

  local tarball="$HOME/${session_id}-preview-session.tar.gz"
  echo "[package] creating $tarball"
  tar -czf "$tarball" -C "$PREVIEW_ROOT/data/sessions" "$session_id"
  ls -lh "$tarball"
  tar -tzf "$tarball" | grep -E 'mesh/mesh_sequence.pt|ik/estimated-dynamics.json|ik/opensim-motion.json|manifest.json' || true
}

main() {
  for session_id in "${!SESSIONS[@]}"; do
    video="${SESSIONS[$session_id]}"
    echo "======== $session_id ($video) ========"
    if [[ ! -d "$DATA_DIR/$session_id" ]]; then
      echo "missing session dir for $session_id" >&2
      exit 1
    fi
    run_dynamics "$session_id"
    package_session "$session_id"
  done
  echo "[done] tarballs in $HOME"
  ls -lh "$HOME"/*-preview-session.tar.gz 2>/dev/null || true
}

main "$@"
