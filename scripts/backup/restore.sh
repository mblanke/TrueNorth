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
PG_DUMP="${BACKUP_PATH}/postgresql.sql.gz"
if [[ -f "${PG_DUMP}" ]]; then
    log "INFO" "Restoring PostgreSQL..."
    gunzip -c "${PG_DUMP}" | docker exec -i truenorth-postgres psql -U truenorth -d truenorth_range
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
    docker stop truenorth-redis 2>/dev/null || true
    docker cp "${REDIS_DUMP}" truenorth-redis:/data/dump.rdb
    docker start truenorth-redis
    sleep 3
    COMPLETED+=("Redis")
    log "INFO" "Redis restore complete"
else
    log "WARN" "No Redis dump found — skipping"
fi

# ── 3. MinIO ─────────────────────────────────────────────────────────────────
CURRENT_STEP="MinIO"
MINIO_DIR="${BACKUP_PATH}/minio"
if [[ -d "${MINIO_DIR}" ]]; then
    log "INFO" "Restoring MinIO..."
    docker run --rm --network host \
        -v "${MINIO_DIR}:/backup" \
        minio/mc:latest sh -c \
        'mc alias set dst http://minio:9000 minioadmin minioadmin && mc mirror /backup/ dst/'
    COMPLETED+=("MinIO")
    log "INFO" "MinIO restore complete"
else
    log "WARN" "No MinIO backup found — skipping"
fi

# ── 4. OpenSearch ────────────────────────────────────────────────────────────
CURRENT_STEP="OpenSearch"
OS_DIR="${BACKUP_PATH}/opensearch-snapshots"
if [[ -d "${OS_DIR}" ]]; then
    log "INFO" "Restoring OpenSearch..."
    docker cp "${OS_DIR}/." truenorth-opensearch:/mnt/snapshots/

    # Register repo
    docker exec truenorth-opensearch curl -s -X PUT \
        "http://localhost:9200/_snapshot/truenorth_backup" \
        -H "Content-Type: application/json" \
        -d '{"type":"fs","settings":{"location":"/mnt/snapshots"}}' > /dev/null

    # Find latest snapshot
    LATEST_SNAP=$(docker exec truenorth-opensearch curl -s \
        "http://localhost:9200/_snapshot/truenorth_backup/_all" \
        | jq -r '.snapshots | sort_by(.start_time_in_millis) | last | .snapshot')

    if [[ -n "${LATEST_SNAP}" && "${LATEST_SNAP}" != "null" ]]; then
        docker exec truenorth-opensearch curl -s -X POST \
            "http://localhost:9200/_all/_close" > /dev/null
        docker exec truenorth-opensearch curl -s -X POST \
            "http://localhost:9200/_snapshot/truenorth_backup/${LATEST_SNAP}/_restore?wait_for_completion=true" > /dev/null
        COMPLETED+=("OpenSearch")
        log "INFO" "OpenSearch restore complete (snapshot: ${LATEST_SNAP})"
    else
        log "WARN" "No snapshots found in backup — skipping OpenSearch restore"
    fi
else
    log "WARN" "No OpenSearch backup found — skipping"
fi

# ── 5. Alembic migrations ───────────────────────────────────────────────────
CURRENT_STEP="Alembic"
log "INFO" "Running Alembic migrations..."
docker exec truenorth-api alembic upgrade head
COMPLETED+=("Alembic")
log "INFO" "Alembic migrations complete"

# ── 6. Health check ─────────────────────────────────────────────────────────
CURRENT_STEP="HealthCheck"
log "INFO" "Running post-restore health check..."
MAX_RETRIES=10
HEALTHY=false
for i in $(seq 1 ${MAX_RETRIES}); do
    if curl -sf --max-time 5 "http://localhost:8080/health" > /dev/null 2>&1; then
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