#!/usr/bin/env bash
# =============================================================================
# TrueNorth Range — Full Platform Backup (Bash)
# =============================================================================
set -euo pipefail
IFS=$'\n\t'

# ── Defaults ─────────────────────────────────────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
ENV_FILE="${ENV_FILE:-.env}"

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

# ── 1. PostgreSQL ────────────────────────────────────────────────────────────
CURRENT_STEP="PostgreSQL"
log "INFO" "Backing up PostgreSQL..."
docker exec truenorth-postgres \
    pg_dump -U truenorth -d truenorth_range --clean --if-exists \
    | gzip > "${BACKUP_PATH}/postgresql.sql.gz"
COMPLETED+=("PostgreSQL")
log "INFO" "PostgreSQL backup complete"

# ── 2. Redis ─────────────────────────────────────────────────────────────────
CURRENT_STEP="Redis"
log "INFO" "Backing up Redis..."
docker exec truenorth-redis redis-cli BGSAVE > /dev/null
sleep 3
docker cp truenorth-redis:/data/dump.rdb "${BACKUP_PATH}/redis-dump.rdb"
COMPLETED+=("Redis")
log "INFO" "Redis backup complete"

# ── 3. MinIO ─────────────────────────────────────────────────────────────────
CURRENT_STEP="MinIO"
log "INFO" "Backing up MinIO..."
MINIO_DIR="${BACKUP_PATH}/minio"
mkdir -p "${MINIO_DIR}"
docker run --rm --network host \
    -v "${MINIO_DIR}:/backup" \
    minio/mc:latest sh -c \
    'mc alias set src http://minio:9000 minioadmin minioadmin && mc mirror src/ /backup/'
COMPLETED+=("MinIO")
log "INFO" "MinIO backup complete"

# ── 4. OpenSearch ────────────────────────────────────────────────────────────
CURRENT_STEP="OpenSearch"
log "INFO" "Backing up OpenSearch via snapshot API..."
# Register repo (idempotent)
docker exec truenorth-opensearch curl -s -X PUT \
    "http://localhost:9200/_snapshot/truenorth_backup" \
    -H "Content-Type: application/json" \
    -d '{"type":"fs","settings":{"location":"/mnt/snapshots"}}' > /dev/null

SNAP_NAME="snap-${TIMESTAMP}"
docker exec truenorth-opensearch curl -s -X PUT \
    "http://localhost:9200/_snapshot/truenorth_backup/${SNAP_NAME}?wait_for_completion=true" > /dev/null

OS_DIR="${BACKUP_PATH}/opensearch-snapshots"
mkdir -p "${OS_DIR}"
docker cp truenorth-opensearch:/mnt/snapshots/. "${OS_DIR}"
COMPLETED+=("OpenSearch")
log "INFO" "OpenSearch backup complete"

# ── 5. Configs ───────────────────────────────────────────────────────────────
CURRENT_STEP="Configs"
log "INFO" "Backing up configuration files..."
CFG_DIR="${BACKUP_PATH}/configs"
mkdir -p "${CFG_DIR}"

for f in "${COMPOSE_FILE}" "docker-compose.dev.yml" "${ENV_FILE}" ".env.example"; do
    [[ -f "$f" ]] && cp "$f" "${CFG_DIR}/"
done

# Nginx configs
[[ -d "infra/platform/nginx" ]] && cp -r "infra/platform/nginx" "${CFG_DIR}/nginx"

# Terraform state
[[ -f "infra/terraform/terraform.tfstate" ]] && cp "infra/terraform/terraform.tfstate" "${CFG_DIR}/"

COMPLETED+=("Configs")
log "INFO" "Config backup complete"

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