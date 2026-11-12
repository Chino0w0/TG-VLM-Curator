#!/usr/bin/env bash
set -euo pipefail

# TG-VLM-Curator PostgreSQL Backup Utility (Section 42)
# Creates a custom-format PostgreSQL dump with recorded UTC timestamps and SHA-256.
# IMPORTANT: Back up APP_MASTER_KEY independently in an offline secret vault.
# Database backups without APP_MASTER_KEY cannot recover encrypted Telegram sessions,
# bot tokens, or external inference API keys.

BACKUP_DIR="${BACKUP_DIR:-./backups/postgres}"
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-tgcurator}"
PGDATABASE="${PGDATABASE:-tgcurator}"

mkdir -p "${BACKUP_DIR}"

STARTED_AT_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
STAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
DUMP_FILE="${BACKUP_DIR}/tgcurator-postgres-${STAMP}.dump"
META_FILE="${DUMP_FILE}.meta.json"

echo "[backup-postgres] Starting PostgreSQL backup at ${STARTED_AT_UTC} -> ${DUMP_FILE}"

pg_dump \
  --format=custom \
  --no-owner \
  --no-acl \
  --host="${PGHOST}" \
  --port="${PGPORT}" \
  --username="${PGUSER}" \
  --dbname="${PGDATABASE}" \
  --file="${DUMP_FILE}"

if [[ ! -s "${DUMP_FILE}" ]]; then
  echo "[backup-postgres] ERROR: Generated dump file is empty: ${DUMP_FILE}" >&2
  exit 1
fi

FINISHED_AT_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
SHA256_HEX="$(sha256sum "${DUMP_FILE}" | awk '{print $1}')"
SIZE_BYTES="$(wc -c < "${DUMP_FILE}" | tr -d ' ')"

cat > "${META_FILE}" <<EOF
{
  "kind": "postgres_backup",
  "database": "${PGDATABASE}",
  "started_at_utc": "${STARTED_AT_UTC}",
  "finished_at_utc": "${FINISHED_AT_UTC}",
  "dump_file": "$(basename "${DUMP_FILE}")",
  "size_bytes": ${SIZE_BYTES},
  "sha256": "${SHA256_HEX}",
  "master_key_included": false,
  "operator_notice": "APP_MASTER_KEY must be preserved independently; encrypted secrets cannot be decrypted without it."
}
EOF

echo "[backup-postgres] Completed at ${FINISHED_AT_UTC} (${SIZE_BYTES} bytes, sha256=${SHA256_HEX})"
echo "[backup-postgres] REMINDER: Ensure APP_MASTER_KEY is stored separately in your offline vault."
