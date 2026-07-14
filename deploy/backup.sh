#!/usr/bin/env bash
# Snapshot the financial record. data/ holds the ledger, holdings, execution
# journals, calibration logs, and operator acks -- the state that cannot be
# recovered from the blockchain alone. Run from the repo root (cron or the
# systemd timer in this directory).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${POLYBOT_BACKUP_DIR:-$REPO_ROOT/backups}"
KEEP="${POLYBOT_BACKUP_KEEP:-14}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARBALL="$BACKUP_DIR/polybot-data-$STAMP.tar.gz"

tar -czf "$TARBALL" -C "$REPO_ROOT" \
    --exclude='data/**/replay' \
    data configs/geopolitics 2>/dev/null || tar -czf "$TARBALL" -C "$REPO_ROOT" data

# Keep the most recent $KEEP snapshots.
ls -1t "$BACKUP_DIR"/polybot-data-*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f

echo "wrote $TARBALL"
