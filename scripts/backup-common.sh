#!/usr/bin/env bash
# Shared host-side Compose operations. Never source an env file as shell code.
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${COMPOSE_PROJECT_NAME:?Set COMPOSE_PROJECT_NAME explicitly; refusing implicit production target}"
[[ "$COMPOSE_PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo 'Invalid project name' >&2; exit 2; }
COMPOSE=(docker compose --project-name "$COMPOSE_PROJECT_NAME" --file "${COMPOSE_FILE:-$ROOT/docker-compose.yml}")
if [[ -n "${COMPOSE_ENV_FILE:-}" ]]; then COMPOSE+=(--env-file "$COMPOSE_ENV_FILE"); fi
compose() { "${COMPOSE[@]}" "$@"; }
require_maintenance() {
  local service running
  running="$(compose ps --status running --services)"
  while IFS= read -r service; do
    case "$service" in postgres|redis|archive-tools|'') ;;
      *) echo "Stop application services before backup/restore: $service" >&2; exit 2 ;;
    esac
  done <<< "$running"
}
checksum() {
  (cd "$(dirname "$1")" && sha256sum "$(basename "$1")" > "$(basename "$1").sha256")
}
verify_checksum() {
  [[ -s "$1" && -s "$1.sha256" ]] || { echo 'Backup or checksum missing/empty' >&2; exit 2; }
  # Read the digest only; never trust paths supplied by a checksum sidecar.
  local expected actual
  read -r expected _ < "$1.sha256"
  [[ "$expected" =~ ^[a-fA-F0-9]{64}$ ]] || { echo 'Invalid checksum sidecar' >&2; exit 2; }
  actual="$(sha256sum "$1")"; actual="${actual%% *}"
  [[ "$actual" == "$expected" ]] || { echo 'Backup checksum mismatch' >&2; exit 2; }
}
metadata() {
  local artifact="$1" kind="$2" digest size
  digest="$(sha256sum "$artifact")"; digest="${digest%% *}"
  size="$(wc -c < "$artifact" | tr -d '[:space:]')"
  printf '{"kind":"%s","started_at_utc":"%s","finished_at_utc":"%s","size_bytes":%s,"sha256":"%s","master_key_included":false,"maintenance_window":true}\n' \
    "$kind" "$STARTED_AT_UTC" "$(date -u +%FT%TZ)" "$size" "$digest" > "$artifact.meta.json"
}
