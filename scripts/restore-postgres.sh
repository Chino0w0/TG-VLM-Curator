#!/usr/bin/env bash
set -euo pipefail
CONFIRMED=false
DUMP_FILE=''
MEDIA_FILE=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm-restore|--yes-restore) CONFIRMED=true; shift ;;
    --media) [[ $# -ge 2 ]] || exit 2; MEDIA_FILE="$2"; shift 2 ;;
    --*) echo "Unknown option: $1" >&2; exit 2 ;;
    *) [[ -z "$DUMP_FILE" ]] || exit 2; DUMP_FILE="$1"; shift ;;
  esac
done
if [[ "$CONFIRMED" != true || -z "$DUMP_FILE" || -z "$MEDIA_FILE" ]]; then
  echo 'Usage: COMPOSE_PROJECT_NAME=<target> restore-postgres.sh --confirm-restore <dump> --media <archive.tar.gz>' >&2
  exit 2
fi
source "$(dirname "$0")/backup-common.sh"
verify_checksum "$DUMP_FILE"
verify_checksum "$MEDIA_FILE"
# Reject path traversal and links before touching either durable store.
python - "$MEDIA_FILE" <<'PY'
import sys, tarfile
from pathlib import PurePosixPath
with tarfile.open(sys.argv[1], 'r:gz') as archive:
    for item in archive:
        path = PurePosixPath(item.name)
        if path.is_absolute() or '..' in path.parts or chr(92) in item.name or not (item.isfile() or item.isdir()):
            raise SystemExit('Unsafe archive member; restore refused')
PY
require_maintenance
# Never overlay media. Restore into a fresh project/volume; existing files are a hard failure.
compose run --rm -T --no-deps archive-tools sh -eu -c '[ -z "$(find /archive -mindepth 1 -print -quit)" ] || { echo "Archive target is not empty" >&2; exit 2; }'
# Transactional SQL restore: a SQL error rolls back database changes and returns nonzero.
compose exec -T postgres sh -eu -c 'exec pg_restore --single-transaction --exit-on-error --clean --if-exists --no-owner --no-acl --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' < "$DUMP_FILE"
compose run --rm -T --no-deps archive-tools sh -eu -c 'tar -xzf - -C /archive; chown -R 10001:10001 /archive' < "$MEDIA_FILE"
# The separately recovered master key must already be mounted. Do not copy keys into backups.
# This read-only scanner is mandatory; discrepancies or missing dependencies fail the command.
REPORT="${RESTORE_REPORT:-./restore-consistency.json}"
compose run --rm -T --no-deps --entrypoint python api -m apps.worker.consistency_cli --archive-root /var/lib/tgcurator/archive --verify-sha256 --json > "$REPORT"
printf '[restore-postgres] Complete; review consistency report: %s\n' "$REPORT"
