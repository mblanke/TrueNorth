#!/usr/bin/env bash
# =============================================================================
# TrueNorth Range — platform restore (counterpart of backup.sh)
# =============================================================================
# Usage: restore.sh <backup-dir> [--force] [--no-restart] [--skip-postgres] [--skip-minio]
#
# Targets the compose project given by COMPOSE_FILE / ENV_FILE (lib.sh), exactly
# like backup.sh. Steps:
#   1. verify SHA256SUMS and manifest.json;
#   2. stop every running service except postgres and minio (nothing may hold a
#      connection to a database being replaced);
#   3. postgres: re-create roles from globals.sql (existing roles are kept, their
#      attributes and passwords reset to the backup's), then for each
#      postgres/<db>.dump: DROP DATABASE ... WITH (FORCE) and pg_restore --create;
#   4. minio: create each bucket if missing, mirror its objects back (--overwrite;
#      objects created after the backup are left in place);
#   5. start the services stopped in step 2 again (unless --no-restart).
#
# It does NOT restore the env file: if it was lost, recover it from the escrow
# first (docs/runbooks/backup-restore.md). It does NOT run migrations: if the code
# is newer than the backup, follow docs/runbooks/upgrade.md afterwards.
# =============================================================================
set -euo pipefail
IFS=$'\n\t'
umask 077

# shellcheck source=scripts/backup/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
TN_LOG_TAG="truenorth-restore"

usage() { echo "Usage: $0 <backup-dir> [--force] [--no-restart] [--skip-postgres] [--skip-minio]" >&2; exit 2; }
[[ $# -ge 1 ]] || usage
BACKUP_PATH="$1"; shift
FORCE=0; RESTART=1; DO_PG=1; DO_MINIO=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=1 ;;
        --no-restart) RESTART=0 ;;
        --skip-postgres) DO_PG=0 ;;
        --skip-minio) DO_MINIO=0 ;;
        *) usage ;;
    esac
    shift
done

CURRENT_STEP="validate"
STOPPED=()
on_exit() {
    local rc=$?
    if (( rc != 0 )); then
        alert "restore FAILED at step '${CURRENT_STEP}' (exit ${rc}) from ${BACKUP_PATH}"
        if (( ${#STOPPED[@]} > 0 )); then
            log "WARN" "services left stopped: ${STOPPED[*]}" >&2
        fi
    fi
}
trap on_exit EXIT

[[ -d "${BACKUP_PATH}" ]] || die "backup directory not found: ${BACKUP_PATH}"
BACKUP_PATH="$(cd "${BACKUP_PATH}" && pwd)"
[[ -f "${BACKUP_PATH}/SHA256SUMS" && -f "${BACKUP_PATH}/manifest.json" ]] \
    || die "${BACKUP_PATH} has no SHA256SUMS/manifest.json — not a backup.sh backup"
log "INFO" "Verifying checksums..."
(cd "${BACKUP_PATH}" && sha256_check) || die "checksum verification FAILED"
log "INFO" "Checksums OK"

compose_init
PG_USER="$(env_get POSTGRES_USER)"
PG_DB="$(env_get POSTGRES_DB)"
[[ -n "${PG_USER}" && -n "${PG_DB}" ]] || die "POSTGRES_USER / POSTGRES_DB missing from ${ENV_FILE}"
PROJECT="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$(service_cid postgres)")"

if (( ! FORCE )); then
    echo "This OVERWRITES the databases and buckets of compose project '${PROJECT}' with:"
    echo "  ${BACKUP_PATH}"
    read -rp "Type the project name (${PROJECT}) to proceed: " answer
    [[ "${answer}" == "${PROJECT}" ]] || { log "INFO" "Restore cancelled"; exit 0; }
fi

# ── Quiesce: stop everything that could hold a connection ───────────────────
CURRENT_STEP="quiesce"
while IFS= read -r svc; do
    case "$svc" in postgres|minio|"") ;; *) STOPPED+=("$svc") ;; esac
done < <(dc ps --services --status running)
if (( ${#STOPPED[@]} > 0 )); then
    log "INFO" "Stopping: ${STOPPED[*]}"
    dc stop "${STOPPED[@]}"
fi

psql_admin() {
    dc exec -T postgres psql -U "${PG_USER}" -d template1 -v ON_ERROR_STOP=1 -q "$@"
}

# ── PostgreSQL ───────────────────────────────────────────────────────────────
if (( DO_PG )); then
    CURRENT_STEP="postgres:globals"
    log "INFO" "Restoring roles..."
    # CREATE ROLE of a role that already exists (the bootstrap superuser, roles the
    # init script made) is turned into a no-op; the ALTER ROLE that follows still
    # applies the backup's attributes. Everything else must succeed.
    # shellcheck disable=SC2016  # $tn$ is SQL dollar quoting, not a shell expansion
    sed -E 's/^CREATE ROLE (.+);$/DO $tn$ BEGIN CREATE ROLE \1; EXCEPTION WHEN duplicate_object THEN NULL; END $tn$;/' \
        "${BACKUP_PATH}/postgres/globals.sql" | psql_admin -f - >/dev/null

    for dump in "${BACKUP_PATH}"/postgres/*.dump; do
        [[ -f "$dump" ]] || continue
        db="$(basename "$dump" .dump)"
        CURRENT_STEP="postgres:${db}"
        log "INFO" "Restoring database ${db}..."
        qdb="\"${db//\"/\"\"}\""
        psql_admin -c "DROP DATABASE IF EXISTS ${qdb} WITH (FORCE)"
        dc exec -T postgres pg_restore -U "${PG_USER}" -d template1 --create --exit-on-error <"$dump"
    done
fi

# ── MinIO ────────────────────────────────────────────────────────────────────
if (( DO_MINIO )) && [[ -d "${BACKUP_PATH}/minio" ]]; then
    for dir in "${BACKUP_PATH}"/minio/*/; do
        [[ -d "$dir" ]] || continue
        bucket="$(basename "$dir")"
        CURRENT_STEP="minio:${bucket}"
        log "INFO" "Restoring bucket ${bucket}..."
        mc_run "${BACKUP_PATH}/minio" mb --ignore-existing "tn/${bucket}" >/dev/null
        mc_run "${BACKUP_PATH}/minio" mirror --quiet --overwrite "/backup/${bucket}" "tn/${bucket}" >/dev/null
    done
fi

# ── Restart ──────────────────────────────────────────────────────────────────
CURRENT_STEP="restart"
if (( RESTART )) && (( ${#STOPPED[@]} > 0 )); then
    log "INFO" "Starting: ${STOPPED[*]}"
    dc start "${STOPPED[@]}"
    STOPPED=()
fi

CURRENT_STEP="done"
log "INFO" "Restore complete from ${BACKUP_PATH}"
if grep -q '"opensearch": {"status":"snapshot"' "${BACKUP_PATH}/manifest.json"; then
    log "INFO" "OpenSearch: this backup references a snapshot; restore it with the _restore API (runbook)"
fi
log "INFO" "Next: if the deployed code is newer than the backup, run migrations (docs/runbooks/upgrade.md)"