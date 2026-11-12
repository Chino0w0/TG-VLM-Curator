#!/usr/bin/env bash
set -euo pipefail

# TG-VLM-Curator Archive Media Volume Backup Utility (Section 42)
# Snapshots normalized immutable archive blobs (`archive-data`) with recorded UTC
# timestamps. PostgreSQL and media backups do not require identical instants; after
# restore, run `tgcurator-check-consistency` to reconcile READY assets and orphans.

ARCHIVE_ROOT="${TGCURATOR_ARCHIVE_ROOT:-/var/lib/tgcurator/archive}"
BACKUP_DIR="${BACKUP_DIR:-./backups/media}"

if [[ ! -d "${ARCHIVE_ROOT}" ]]; then
  echo "[backup-media] ERROR: ARCHIVE_ROOT does not exist: ${ARCHIVE_ROOT}" >&2
  exit 1
fi

mkdir -p "${BACKUP_DIR}"

STARTED_AT_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
STAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
TAR_FILE="${BACKUP_DIR}/tgcurator-archive-${STAMP}.tar.gz"
META_FILE="${TAR_FILE}.meta.json"

echo "[backup-media] Archiving ${ARCHIVE_ROOT} at ${STARTED_AT_UTC} -> ${TAR_FILE}"

tar \
  --exclude='.tgcurator-archive-*.tmp' \
  -czf "${TAR_FILE}" \
  -C "${ARCHIVE_ROOT}" \
  .

FINISHED_AT_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
SHA256_HEX="$(sha256sum "${TAR_FILE}" | awk '{print $1}')"
SIZE_BYTES="$(wc -c < "${TAR_FILE}" | tr -d ' ')"

cat > "${META_FILE}" <<EOF
{
  "kind": "media_archive_backup",
  "archive_root": "${ARCHIVE_ROOT}",
  "started_at_utc": "${STARTED_AT_UTC}",
  "finished_at_utc": "${FINISHED_AT_UTC}",
  "archive_file": "$(basename "${TAR_FILE}")",
  "size_bytes": ${SIZE_BYTES},
  "sha256": "${SHA256_HEX}"
}
EOF

echo "[backup-media] Completed at ${FINISHED_AT_UTC} (${SIZE_BYTES} bytes, sha256=${SHA256_HEX})"
