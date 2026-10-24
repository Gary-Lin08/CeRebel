#!/usr/bin/env bash
# Daily backup of data/product (accounts, subscriptions, usage events) to /var/backups/cerebel.
# Installed to /usr/local/sbin/cerebel-backup and run by /etc/cron.d/cerebel-backup.
# Keeps 14 daily copies. Also prunes deploy backups (.deploy-backup/) older than 14 days.
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/cerebel}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/cerebel}"
KEEP_DAYS="${KEEP_DAYS:-14}"
SRC="$APP_DIR/data/product"

umask 077
mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y-%m-%dT%H%MZ)"
out="$BACKUP_DIR/product-$stamp.tar.gz"
if [[ -d "$SRC" ]]; then
  # Files may change while we read them (events.jsonl appends); tar exit 1 means just that.
  rc=0
  # GNU tar (Linux) knows --warning; macOS bsdtar does not and treats it as an error.
  warn_flag=()
  tar --version 2>/dev/null | grep -q 'GNU tar' && warn_flag=(--warning=no-file-changed)
  tar -czf "$out.part" ${warn_flag[@]+"${warn_flag[@]}"} -C "$APP_DIR/data" product || rc=$?
  if [[ $rc -gt 1 ]]; then
    rm -f "$out.part"
    echo "$(date -u +%FT%TZ) backup FAILED rc=$rc" >&2
    exit "$rc"
  fi
  mv "$out.part" "$out"
  echo "$(date -u +%FT%TZ) backup ok $out ($(du -h "$out" | cut -f1))"
else
  echo "$(date -u +%FT%TZ) nothing to back up: $SRC does not exist"
fi
find "$BACKUP_DIR" -maxdepth 1 -name 'product-*.tar.gz' -mtime +"$((KEEP_DAYS - 1))" -delete
if [[ -d "$APP_DIR/.deploy-backup" ]]; then
  find "$APP_DIR/.deploy-backup" -mindepth 1 -maxdepth 1 -type d -mtime +"$((KEEP_DAYS - 1))" \
    -exec rm -rf {} +
fi
