#!/usr/bin/env bash
set -euo pipefail

# TG-VLM-Curator PostgreSQL Restore & Post-Restore Consistency Audit (Section 42)
# Usage:
#   scripts/restore-postgres.sh --confirm-restore <path-to-dump> [--archive-root /var/lib/tgcurator/archive]

CONFIRM_FLAG=""
DUMP_FILE=""
ARCHIVE_ROOT="${TGCURATOR_ARCHIVE_ROOT:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm-restore)
      CONFIRM_FLAG="yes"
      shift
      ;;
    --archive-root)
      ARCHIVE_ROOT="$2"
      shift 2
      ;;
    *)
      if [[ -z "${DUMP_FILE}" ]]; then
        DUMP_FILE="$1"
        shift
      else
        echo "[restore-postgres] Unknown argument: $1" >&2
        exit 2
      fi
      ;;
  esac
done

if [[ "${CONFIRM_FLAG}" != "yes" || -z "${DUMP_FILE}" ]]; then
  echo "Usage: $0 --confirm-restore <dump-file> [--archive-root <dir>]" >&2
  exit 2
fi

if [[ ! -f "${DUMP_FILE}" ]]; then
  echo "[restore-postgres] ERROR: Dump file not found: ${DUMP_FILE}" >&2
  exit 1
fi

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-tgcurator}"
PGDATABASE="${PGDATABASE:-tgcurator}"

RESTORED_AT_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "[restore-postgres] Restoring ${DUMP_FILE} into ${PGDATABASE} at ${RESTORED_AT_UTC}"

pg_restore \
  --clean \
  --if-exists \
  --no-owner \
  --no-acl \
  --host="${PGHOST}" \
  --port="${PGPORT}" \
  --username="${PGUSER}" \
  --dbname="${PGDATABASE}" \
  "${DUMP_FILE}"

echo "[restore-postgres] Database restore finished. Ensure matching APP_MASTER_KEY is mounted."

if [[ -n "${ARCHIVE_ROOT}" && -d "${ARCHIVE_ROOT}" ]]; then
  echo "[restore-postgres] Running post-restore Maintenance Storage Consistency Scanner..."
  python -m apps.worker.consistency_cli --archive-root "${ARCHIVE_ROOT}" --json
else
  echo "[restore-postgres] NOTE: Run 'python -m apps.worker.consistency_cli --archive-root <dir>' to verify READY archive blobs and orphan files."
fi
