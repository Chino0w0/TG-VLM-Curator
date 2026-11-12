#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/backup-common.sh"
# APP_MASTER_KEY must be preserved independently in an offline secret vault.
# started_at_utc is recorded by metadata(); no credentials are printed.
require_maintenance
BACKUP_DIR="${BACKUP_DIR:-./backups/postgres}"
mkdir -p "$BACKUP_DIR"
STARTED_AT_UTC="$(date -u +%FT%TZ)"
DUMP_FILE="$(mktemp "$BACKUP_DIR/tgcurator-postgres-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.dump")"
COMPLETE=false
trap 'if [[ "$COMPLETE" != true ]]; then rm -f -- "$DUMP_FILE" "$DUMP_FILE.partial" "$DUMP_FILE.sha256" "$DUMP_FILE.meta.json"; fi' EXIT
# Run the matching pg_dump16 inside postgres16; no host port/client is required.
compose exec -T postgres sh -eu -c 'exec pg_dump --format=custom --no-owner --no-acl --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' > "$DUMP_FILE.partial"
[[ -s "$DUMP_FILE.partial" ]] || { echo 'Empty dump' >&2; exit 1; }
mv -- "$DUMP_FILE.partial" "$DUMP_FILE"
checksum "$DUMP_FILE"
metadata "$DUMP_FILE" postgres_backup
COMPLETE=true
printf '[backup-postgres] Completed: %s\n' "$DUMP_FILE"
