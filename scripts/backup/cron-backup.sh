#!/usr/bin/env bash
# =============================================================================
# TrueNorth Range — Cron Backup Wrapper
# =============================================================================
#
# Schedule via crontab:
#   0 2 * * * /opt/truenorth/scripts/backup/cron-backup.sh >> /var/log/truenorth-backup.log 2>&1
#
# Rotation policy:
#   - 7 daily backups
#   - 4 weekly backups (Sundays)
#   - 12 monthly backups (1st of month)
#
# Environment variables (backup.sh's are passed through — COMPOSE_FILE, ENV_FILE,
# BACKUP_ESCROW_PUBKEY / BACKUP_ESCROW, ... see docs/runbooks/backup-restore.md):
#   BACKUP_DIR       — Root backup directory (default: /srv/truenorth/backups)
#   S3_BUCKET        — Remote S3 bucket for offsite copies (optional)
#   BACKUP_WEBHOOK_URL / WEBHOOK_URL — optional extra alert channel. Failures
#                      always alert on stderr and syslog without it.
#   DAILY_KEEP       — Number of daily backups to keep (default: 7)
#   WEEKLY_KEEP      — Number of weekly backups to keep (default: 4)
#   MONTHLY_KEEP     — Number of monthly backups to keep (default: 12)
#
# Exits non-zero whenever backup.sh does (1 = failed, 3 = no secrets escrow).
# =============================================================================
set -euo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/backup/lib.sh
source "${SCRIPT_DIR}/lib.sh"
BACKUP_DIR="${BACKUP_DIR:-/srv/truenorth/backups}"
S3_BUCKET="${S3_BUCKET:-}"
DAILY_KEEP="${DAILY_KEEP:-7}"
WEEKLY_KEEP="${WEEKLY_KEEP:-4}"
MONTHLY_KEEP="${MONTHLY_KEEP:-12}"

send_notification() {
    local status="$1"
    local message="$2"
    if [[ "${status}" == "failure" ]]; then
        alert "${message}"
    else
        log "INFO" "${message}"
    fi
}

# ── Determine backup type ───────────────────────────────────────────────────
DAY_OF_WEEK=$(date +%u)   # 1=Monday, 7=Sunday
DAY_OF_MONTH=$(date +%d)  # 01-31

BACKUP_TYPE="daily"
if [[ "${DAY_OF_MONTH}" == "01" ]]; then
    BACKUP_TYPE="monthly"
elif [[ "${DAY_OF_WEEK}" == "7" ]]; then
    BACKUP_TYPE="weekly"
fi

log "INFO" "=== TrueNorth Range Cron Backup (${BACKUP_TYPE}) ==="

# ── Create type-specific backup directory ────────────────────────────────────
TYPE_DIR="${BACKUP_DIR}/${BACKUP_TYPE}"
mkdir -p "${TYPE_DIR}"

# ── Run backup ───────────────────────────────────────────────────────────────
BACKUP_START=$(date +%s)

# Age-based pruning inside backup.sh is off here: this wrapper rotates by count per
# type, and a 30-day age limit would silently delete every monthly backup.
BACKUP_RC=0
# BACKUP_ROOT: the free-space and size-cap checks see every type directory, not just this one.
ROOT_DIR="${BACKUP_DIR}"
BACKUP_ROOT="${ROOT_DIR}" BACKUP_DIR="${TYPE_DIR}" BACKUP_RETENTION_DAYS=0 \
    bash "${SCRIPT_DIR}/backup.sh" || BACKUP_RC=$?
if (( BACKUP_RC != 0 && BACKUP_RC != 3 )); then
    DURATION=$(( $(date +%s) - BACKUP_START ))
    send_notification "failure" "Backup type: ${BACKUP_TYPE}. Failed (exit ${BACKUP_RC}) after ${DURATION}s."
    exit "${BACKUP_RC}"
fi

DURATION=$(( $(date +%s) - BACKUP_START ))
log "INFO" "Backup completed in ${DURATION}s"

# ── Find the latest backup just created ──────────────────────────────────────
LATEST_BACKUP=$(find "${TYPE_DIR}" -maxdepth 1 -type d -name "truenorth-backup-*" | sort | tail -1)

# ── Upload to S3 if configured ──────────────────────────────────────────────
if [[ -n "${S3_BUCKET}" && -n "${LATEST_BACKUP}" ]]; then
    log "INFO" "Uploading to S3: ${S3_BUCKET}/${BACKUP_TYPE}/..."
    # S3_ENCRYPT=1 GPG-encrypts the archive (GPG_RECIPIENT) before it leaves the host.
    if ! bash "${SCRIPT_DIR}/s3-backup.sh" "${LATEST_BACKUP}" "${S3_BUCKET}/${BACKUP_TYPE}" ${S3_ENCRYPT:+--encrypt} 2>&1; then
        log "WARN" "S3 upload failed — local backup is intact"
        send_notification "failure" "Local backup succeeded but S3 upload failed (${BACKUP_TYPE})."
        OFFSITE_FAILED=1
    fi
fi

# ── Rotation ─────────────────────────────────────────────────────────────────
rotate_backups() {
    local dir="$1"
    local keep="$2"
    local type_name="$3"

    if [[ ! -d "${dir}" ]]; then
        return 0
    fi

    local count
    count=$(find "${dir}" -maxdepth 1 -type d -name "truenorth-backup-*" | wc -l)

    if (( count > keep )); then
        local to_delete=$(( count - keep ))
        log "INFO" "Rotating ${type_name}: removing ${to_delete} old backup(s) (keeping ${keep})"
        find "${dir}" -maxdepth 1 -type d -name "truenorth-backup-*" \
            | sort \
            | head -n "${to_delete}" \
            | xargs rm -rf
    fi
}

rotate_backups "${BACKUP_DIR}/daily"   "${DAILY_KEEP}"   "daily"
rotate_backups "${BACKUP_DIR}/weekly"  "${WEEKLY_KEEP}"  "weekly"
rotate_backups "${BACKUP_DIR}/monthly" "${MONTHLY_KEEP}" "monthly"
# The size cap (BACKUP_MAX_TOTAL_GB) over daily/weekly/monthly/pre-upgrade together:
# count-based rotation alone lets big backups fill the disk.
prune_to_size "${BACKUP_DIR}" "${BACKUP_MAX_TOTAL_GB:-0}" "${LATEST_BACKUP}"

# ── Summary ──────────────────────────────────────────────────────────────────
# A type directory exists only once that type has run (weekly/monthly not in a host's
# first week): find on it fails, and under pipefail + errexit that failed a backup that
# had succeeded (exit 1, failure alert) every night until then.
count_backups() {
    [[ -d "$1" ]] || { echo 0; return 0; }
    find "$1" -maxdepth 1 -type d -name "truenorth-backup-*" | wc -l
}
DAILY_COUNT=$(count_backups "${BACKUP_DIR}/daily")
WEEKLY_COUNT=$(count_backups "${BACKUP_DIR}/weekly")
MONTHLY_COUNT=$(count_backups "${BACKUP_DIR}/monthly")
TOTAL_SIZE=$(du -sh "${BACKUP_DIR}" 2>/dev/null | cut -f1)

SUMMARY="Type: ${BACKUP_TYPE} | Duration: ${DURATION}s | Daily: ${DAILY_COUNT}/${DAILY_KEEP} | Weekly: ${WEEKLY_COUNT}/${WEEKLY_KEEP} | Monthly: ${MONTHLY_COUNT}/${MONTHLY_KEEP} | Total: ${TOTAL_SIZE}"
log "INFO" "${SUMMARY}"

log "INFO" "=== Cron backup complete ==="
if (( BACKUP_RC != 0 )); then
    exit "${BACKUP_RC}"   # 3: data backed up, secrets not escrowed (already alerted)
fi
if (( ${OFFSITE_FAILED:-0} )); then
    exit 4
fi