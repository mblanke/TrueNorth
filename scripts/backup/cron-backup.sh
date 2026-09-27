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
# Environment variables:
#   BACKUP_DIR       — Root backup directory (default: /opt/truenorth/backups)
#   S3_BUCKET        — Remote S3 bucket for offsite copies (optional)
#   WEBHOOK_URL      — Notification webhook for failures (Slack/Teams/Discord)
#   DAILY_KEEP       — Number of daily backups to keep (default: 7)
#   WEEKLY_KEEP      — Number of weekly backups to keep (default: 4)
#   MONTHLY_KEEP     — Number of monthly backups to keep (default: 12)
#
# =============================================================================
set -euo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/opt/truenorth/backups}"
S3_BUCKET="${S3_BUCKET:-}"
WEBHOOK_URL="${WEBHOOK_URL:-}"
DAILY_KEEP="${DAILY_KEEP:-7}"
WEEKLY_KEEP="${WEEKLY_KEEP:-4}"
MONTHLY_KEEP="${MONTHLY_KEEP:-12}"

# ── Helpers ──────────────────────────────────────────────────────────────────
log() {
    local level="$1"; shift
    printf '[%s] [%s] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")" "$level" "$*"
}

send_notification() {
    local status="$1"
    local message="$2"

    if [[ -z "${WEBHOOK_URL}" ]]; then
        return 0
    fi

    local color="good"
    [[ "${status}" == "failure" ]] && color="danger"

    local payload
    payload=$(cat <<EOF
{
    "text": "TrueNorth Range Backup ${status^^}",
    "attachments": [{
        "color": "${color}",
        "fields": [{
            "title": "Status",
            "value": "${status}",
            "short": true
        }, {
            "title": "Host",
            "value": "$(hostname)",
            "short": true
        }, {
            "title": "Details",
            "value": "${message}",
            "short": false
        }, {
            "title": "Timestamp",
            "value": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
            "short": true
        }]
    }]
}
EOF
)

    curl -sf -X POST -H "Content-Type: application/json" \
        -d "${payload}" "${WEBHOOK_URL}" > /dev/null 2>&1 || \
        log "WARN" "Failed to send notification webhook"
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

if ! BACKUP_DIR="${TYPE_DIR}" bash "${SCRIPT_DIR}/backup.sh" 2>&1; then
    DURATION=$(( $(date +%s) - BACKUP_START ))
    log "ERROR" "Backup failed after ${DURATION}s"
    send_notification "failure" "Backup type: ${BACKUP_TYPE}. Failed after ${DURATION}s."
    exit 1
fi

DURATION=$(( $(date +%s) - BACKUP_START ))
log "INFO" "Backup completed in ${DURATION}s"

# ── Find the latest backup just created ──────────────────────────────────────
LATEST_BACKUP=$(find "${TYPE_DIR}" -maxdepth 1 -type d -name "truenorth-backup-*" | sort | tail -1)

# ── Upload to S3 if configured ──────────────────────────────────────────────
if [[ -n "${S3_BUCKET}" && -n "${LATEST_BACKUP}" ]]; then
    log "INFO" "Uploading to S3: ${S3_BUCKET}/${BACKUP_TYPE}/..."
    if ! bash "${SCRIPT_DIR}/s3-backup.sh" "${LATEST_BACKUP}" "${S3_BUCKET}/${BACKUP_TYPE}" 2>&1; then
        log "WARN" "S3 upload failed — local backup is intact"
        send_notification "failure" "Local backup succeeded but S3 upload failed (${BACKUP_TYPE})."
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

# ── Summary ──────────────────────────────────────────────────────────────────
DAILY_COUNT=$(find "${BACKUP_DIR}/daily"   -maxdepth 1 -type d -name "truenorth-backup-*" 2>/dev/null | wc -l)
WEEKLY_COUNT=$(find "${BACKUP_DIR}/weekly"  -maxdepth 1 -type d -name "truenorth-backup-*" 2>/dev/null | wc -l)
MONTHLY_COUNT=$(find "${BACKUP_DIR}/monthly" -maxdepth 1 -type d -name "truenorth-backup-*" 2>/dev/null | wc -l)
TOTAL_SIZE=$(du -sh "${BACKUP_DIR}" 2>/dev/null | cut -f1)

SUMMARY="Type: ${BACKUP_TYPE} | Duration: ${DURATION}s | Daily: ${DAILY_COUNT}/${DAILY_KEEP} | Weekly: ${WEEKLY_COUNT}/${WEEKLY_KEEP} | Monthly: ${MONTHLY_COUNT}/${MONTHLY_KEEP} | Total: ${TOTAL_SIZE}"
log "INFO" "${SUMMARY}"

send_notification "success" "${SUMMARY}"
log "INFO" "=== Cron backup complete ==="