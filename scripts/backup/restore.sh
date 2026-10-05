#!/usr/bin/env bash
# =============================================================================
# TrueNorth Range — Full Platform Restore (Bash)
# =============================================================================
set -euo pipefail
IFS=$'\n\t'

# ── Arguments ────────────────────────────────────────────────────────────────
BACKUP_PATH="${1:?Usage: $0 <backup-directory> [--force]}"
FORCE="${2:-}"

# ── Helpers ──────────────────────────────────────────────────────────────────
log() {
    local level="$1"; shift
    printf '[%s] [%s] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")" "$level" "$*"
}

cleanup_on_error() {
    log "ERROR" "Restore FAILED at step: ${CURRENT_STEP:-unknown}"
    log "WARN"  "Completed before failure: ${COMPLETED[*]:-none}"
    exit 1
}
trap cleanup_on_error ERR

COMPLETED=()
COMPOSE_FILE="${COMPOSE_FILE:-infra/platform/docker/compose.prod.yml}"
ENV_FILE="${ENV_FILE:-infra/platform/docker/.env.production}"
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# ── Validate ─────────────────────────────────────────────────────────────────
if [[ ! -d "${BACKUP_PATH}" ]]; then
    log "ERROR" "Backup path not found: ${BACKUP_PATH}"
    exit 1
fi

MANIFEST="${BACKUP_PATH}/SHA256SUMS.txt"
if [[ ! -f "${MANIFEST}" ]]; then
    log "ERROR" "SHA256SUMS.txt not found — cannot verify integrity"
    exit 1
fi

# ── Verify checksums ────────────────────────────────────────────────────────
log "INFO" "Verifying SHA-256 checksums..."
if ! (cd "${BACKUP_PATH}" && sha256sum -c SHA256SUMS.txt); then
    log "ERROR" "Checksum verification FAILED. Aborting."
    exit 1
fi
log "INFO" "All checksums verified successfully"

# ── Confirmation ─────────────────────────────────────────────────────────────
if [[ "${FORCE}" != "--force" ]]; then
    echo ""
    echo "WARNING: This will OVERWRITE current data with backup from:"
    echo "  ${BACKUP_PATH}"
    echo ""
    read -rp "Type 'YES' to proceed: " answer
    if [[ "${answer}" != "YES" ]]; then
        log "INFO" "Restore cancelled by user"
        exit 0
    fi
fi

# ── 1. PostgreSQL ────────────────────────────────────────────────────────────
CURRENT_STEP="PostgreSQL"
PG_USER="$(envval POSTGRES_USER)"
restore_db() {  # <dump> <database>
    log "INFO" "Restoring PostgreSQL database $2..."
    gunzip -c "$1" | dc exec -T postgres psql -v ON_ERROR_STOP=1 -U "${PG_USER}" -d "$2" > /dev/null
}
if [[ -f "${BACKUP_PATH}/postgresql.sql.gz" ]]; then
    restore_db "${BACKUP_PATH}/postgresql.sql.gz" "$(envval POSTGRES_DB)"
    for dump in "${BACKUP_PATH}"/postgresql-*.sql.gz; do
        [[ -f "${dump}" ]] || continue
        db="${dump##*/postgresql-}"; restore_db "${dump}" "${db%.sql.gz}"
    done
    COMPLETED+=("PostgreSQL")
    log "INFO" "PostgreSQL restore complete"
else
    log "WARN" "No PostgreSQL dump found — skipping"
fi

# ── 2. Redis ─────────────────────────────────────────────────────────────────
CURRENT_STEP="Redis"
REDIS_DUMP="${BACKUP_PATH}/redis-dump.rdb"
if [[ -f "${REDIS_DUMP}" ]]; then
    log "INFO" "Restoring Redis..."
    REDIS_ID="$(cid redis)"
    docker stop "${REDIS_ID}" > /dev/null
    docker cp "${REDIS_DUMP}" "${REDIS_ID}:/data/dump.rdb"
    docker start "${REDIS_ID}" > /dev/null
    sleep 3
    COMPLETED+=("Redis")
    log "INFO" "Redis restore complete"
else
    log "WARN" "No Redis dump found — skipping"
fi

# ── 3. MinIO ─────────────────────────────────────────────────────────────────
CURRENT_STEP="MinIO"
if [[ -f "${BACKUP_PATH}/minio.tar.gz" ]]; then
    log "INFO" "Restoring MinIO..."
    DATA_ROOT="$(envval TN_DATA_ROOT)"
    PG_IMAGE="$(dc config --images | grep -m1 '^postgres')"
    dc stop minio > /dev/null
    docker run --rm -i -v "${DATA_ROOT:-/srv/truenorth}/minio:/data" --entrypoint sh "${PG_IMAGE}" \
        -c 'find /data -mindepth 1 -delete && tar -C /data -xzf -' < "${BACKUP_PATH}/minio.tar.gz"
    dc start minio > /dev/null
    COMPLETED+=("MinIO")
    log "INFO" "MinIO restore complete"
else
    log "WARN" "No MinIO backup found — skipping"
fi

# ── 4. OpenSearch: not in the backup set (see backup.sh) ─────────────────────
log "WARN" "OpenSearch telemetry is not part of the backup set; indexes start empty"

# ── 5. Alembic migrations ───────────────────────────────────────────────────
CURRENT_STEP="Alembic"
log "INFO" "Running Alembic migrations..."
dc exec -T api alembic -c alembic.ini upgrade head
COMPLETED+=("Alembic")
log "INFO" "Alembic migrations complete"

# ── 6. Health check ─────────────────────────────────────────────────────────
CURRENT_STEP="HealthCheck"
log "INFO" "Running post-restore health check..."
MAX_RETRIES=10
HEALTHY=false
for i in $(seq 1 ${MAX_RETRIES}); do
    if dc exec -T api curl -sf --max-time 5 "http://localhost:8080/health" > /dev/null 2>&1; then
        HEALTHY=true
        break
    fi
    log "INFO" "Health check attempt ${i}/${MAX_RETRIES} — waiting..."
    sleep 5
done

if ${HEALTHY}; then
    log "INFO" "Health check PASSED"
else
    log "WARN" "Health check did not pass after ${MAX_RETRIES} attempts — manual verification required"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
log "INFO" "Restore complete. Components restored: ${COMPLETED[*]}"