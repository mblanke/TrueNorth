#!/usr/bin/env bash
# =============================================================================
# TrueNorth Range — Full Platform Backup (Bash)
# =============================================================================
set -euo pipefail
IFS=$'\n\t'

# ── Defaults ─────────────────────────────────────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
# There is no docker-compose.yml at the repo root — the stacks live under
# infra/platform/docker/. The old default silently pointed at a file that has
# never existed, so every `docker compose` call here failed.
COMPOSE_FILE="${COMPOSE_FILE:-infra/platform/docker/compose.prod.yml}"
ENV_FILE="${ENV_FILE:-infra/platform/docker/.env.production}"

# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TIMESTAMP=$(date +"%Y%m%d-%H%M%S")
BACKUP_NAME="truenorth-backup-${TIMESTAMP}"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_NAME}"
MANIFEST="${BACKUP_PATH}/SHA256SUMS.txt"
COMPLETED=()

# ── Helpers ──────────────────────────────────────────────────────────────────
log() {
    local level="$1"; shift
    printf '[%s] [%s] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")" "$level" "$*"
}

cleanup_on_error() {
    log "ERROR" "Backup FAILED at step: ${CURRENT_STEP:-unknown}"
    log "WARN"  "Completed before failure: ${COMPLETED[*]:-none}"
    exit 1
}
trap cleanup_on_error ERR

# ── Setup ────────────────────────────────────────────────────────────────────
log "INFO" "Starting TrueNorth Range backup → ${BACKUP_PATH}"
mkdir -p "${BACKUP_PATH}"

# ── 1. PostgreSQL: the application, Keycloak and LRS databases ─────────────────
CURRENT_STEP="PostgreSQL"
log "INFO" "Backing up PostgreSQL..."
PG_USER="$(envval POSTGRES_USER)"
for db in "$(envval POSTGRES_DB)" "$(envval KEYCLOAK_DB)" "$(envval LRS_DB)"; do
    [[ -n "${db}" ]] || continue
    out="postgresql.sql.gz"
    [[ "${db}" == "$(envval POSTGRES_DB)" ]] || out="postgresql-${db}.sql.gz"
    dc exec -T postgres pg_dump -U "${PG_USER}" -d "${db}" --clean --if-exists | gzip > "${BACKUP_PATH}/${out}"
done
COMPLETED+=("PostgreSQL")
log "INFO" "PostgreSQL backup complete"

# ── 2. Redis ─────────────────────────────────────────────────────────────────
CURRENT_STEP="Redis"
log "INFO" "Backing up Redis..."
rcli() { dc exec -T -e REDISCLI_AUTH="$(envval REDIS_PASSWORD)" redis redis-cli "$@"; }
before="$(rcli LASTSAVE)"
rcli BGSAVE > /dev/null
for _ in $(seq 1 60); do [[ "$(rcli LASTSAVE)" != "${before}" ]] && break; sleep 1; done
docker cp "$(cid redis):/data/dump.rdb" "${BACKUP_PATH}/redis-dump.rdb"
COMPLETED+=("Redis")
log "INFO" "Redis backup complete"

# ── 3. MinIO ─────────────────────────────────────────────────────────────────
# Its data directory, archived with a tool from an image already on the host (MinIO's
# own image has no tar, and minio/mc is no longer published on Docker Hub).
CURRENT_STEP="MinIO"
log "INFO" "Backing up MinIO..."
DATA_ROOT="$(envval TN_DATA_ROOT)"
PG_IMAGE="$(dc config --images | grep -m1 '^postgres')"
docker run --rm -v "${DATA_ROOT:-/srv/truenorth}/minio:/data:ro" --entrypoint tar "${PG_IMAGE}" \
    -C /data -czf - . > "${BACKUP_PATH}/minio.tar.gz"
COMPLETED+=("MinIO")
log "INFO" "MinIO backup complete"

# ── 4. OpenSearch ────────────────────────────────────────────────────────────
# Not backed up: compose.prod.yml configures no snapshot repository (path.repo), so the
# snapshot API has nowhere to write. The indexes hold range telemetry; scores and
# outcomes are in PostgreSQL.
log "WARN" "OpenSearch telemetry is NOT backed up (no snapshot repository is configured)"

# ── 5. Configs ───────────────────────────────────────────────────────────────
# The compose file and nginx config, not the env file: it holds every secret, and a
# backup set is copied to places secrets must not go. Back up the installer's
# config/secrets directory (or the ansible vault) separately, offline.
CURRENT_STEP="Configs"
log "INFO" "Backing up configuration files..."
CFG_DIR="${BACKUP_PATH}/configs"
mkdir -p "${CFG_DIR}"
cp "${COMPOSE_FILE}" "${CFG_DIR}/"
NGINX_DIR="$(dirname "${COMPOSE_FILE}")/../nginx"
[[ -d "${NGINX_DIR}" ]] && cp -r "${NGINX_DIR}" "${CFG_DIR}/nginx"
COMPLETED+=("Configs")
log "INFO" "Config backup complete (secrets excluded: back up the config/secrets directory offline)"

# ── 6. SHA-256 Manifest ─────────────────────────────────────────────────────
CURRENT_STEP="Checksums"
log "INFO" "Generating SHA-256 checksums..."
(cd "${BACKUP_PATH}" && find . -type f ! -name "SHA256SUMS.txt" -exec sha256sum {} \;) > "${MANIFEST}"
log "INFO" "Manifest written: ${MANIFEST}"

# ── 7. Retention cleanup ────────────────────────────────────────────────────
CURRENT_STEP="Retention"
log "INFO" "Cleaning backups older than ${RETENTION_DAYS} days..."
find "${BACKUP_DIR}" -maxdepth 1 -type d -name "truenorth-backup-*" -mtime "+${RETENTION_DAYS}" -exec rm -rf {} +

# ── Done ─────────────────────────────────────────────────────────────────────
TOTAL_SIZE=$(du -sh "${BACKUP_PATH}" | cut -f1)
log "INFO" "Backup complete. Size: ${TOTAL_SIZE}"
log "INFO" "Components backed up: ${COMPLETED[*]}"
log "INFO" "Backup location: ${BACKUP_PATH}"