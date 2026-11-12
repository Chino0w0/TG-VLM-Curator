#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/backup-common.sh"
require_maintenance
BACKUP_DIR="${BACKUP_DIR:-./backups/media}"
mkdir -p "$BACKUP_DIR"
STARTED_AT_UTC="$(date -u +%FT%TZ)"
TAR_FILE="$(mktemp "$BACKUP_DIR/tgcurator-archive-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.tar.gz")"
COMPLETE=false
trap 'if [[ "$COMPLETE" != true ]]; then rm -f -- "$TAR_FILE" "$TAR_FILE.partial" "$TAR_FILE.sha256" "$TAR_FILE.meta.json"; fi' EXIT
# Uses the project's named archive-data volume, not a host /var/lib path.
compose run --rm -T --no-deps archive-tools tar -czf - -C /archive . > "$TAR_FILE.partial"
[[ -s "$TAR_FILE.partial" ]] || { echo 'Empty archive' >&2; exit 1; }
mv -- "$TAR_FILE.partial" "$TAR_FILE"
checksum "$TAR_FILE"
metadata "$TAR_FILE" media_archive_backup
COMPLETE=true
printf '[backup-media] Completed: %s\n' "$TAR_FILE"
