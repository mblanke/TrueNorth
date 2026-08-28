#!/usr/bin/env bash
# =============================================================================
# TrueNorth Range — Upload Backup to S3 / MinIO Remote
# =============================================================================
#
# Usage:
#   ./s3-backup.sh <backup-dir> <s3-bucket> [--encrypt]
#
# Examples:
#   ./s3-backup.sh ./backups/truenorth-backup-20260225-020000 s3://truenorth-backups
#   ./s3-backup.sh ./backups/truenorth-backup-20260225-020000 s3://truenorth-backups --encrypt
#
# Environment variables:
#   S3_ENDPOINT    — Custom S3 endpoint for MinIO (e.g. https://minio.example.com)
#   GPG_RECIPIENT  — GPG recipient for encryption (default: truenorth-backup)
#   AWS_PROFILE    — AWS CLI profile (optional)
#
# Lifecycle policy documentation:
#   For S3, apply a lifecycle rule to the target bucket:
#     - Transition to S3-IA after 30 days
#     - Transition to Glacier after 90 days
#     - Expire after 365 days
#   AWS CLI:
#     aws s3api put-bucket-lifecycle-configuration \
#       --bucket truenorth-backups \
#       --lifecycle-configuration file://lifecycle.json
#
# =============================================================================
set -euo pipefail
IFS=$'\n\t'

BACKUP_DIR="${1:?Usage: $0 <backup-dir> <s3-bucket> [--encrypt]}"
S3_BUCKET="${2:?Usage: $0 <backup-dir> <s3-bucket> [--encrypt]}"
ENCRYPT="${3:-}"
S3_ENDPOINT="${S3_ENDPOINT:-}"
GPG_RECIPIENT="${GPG_RECIPIENT:-truenorth-backup}"

# ── Helpers ──────────────────────────────────────────────────────────────────
log() {
    local level="$1"; shift
    printf '[%s] [%s] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")" "$level" "$*"
}

# ── Validate ─────────────────────────────────────────────────────────────────
if [[ ! -d "${BACKUP_DIR}" ]]; then
    log "ERROR" "Backup directory not found: ${BACKUP_DIR}"
    exit 1
fi

BACKUP_NAME=$(basename "${BACKUP_DIR}")
ARCHIVE="${BACKUP_DIR}.tar.gz"

# ── Build S3/mc command args ─────────────────────────────────────────────────
S3_ARGS=()
if [[ -n "${S3_ENDPOINT}" ]]; then
    S3_ARGS+=("--endpoint-url" "${S3_ENDPOINT}")
fi

# Detect tool: prefer aws cli, fall back to mc
USE_MC=false
if ! command -v aws &> /dev/null; then
    if command -v mc &> /dev/null; then
        USE_MC=true
        log "INFO" "aws cli not found — using MinIO mc client"
    else
        log "ERROR" "Neither 'aws' nor 'mc' found. Install one to continue."
        exit 1
    fi
fi

# ── Create tarball ───────────────────────────────────────────────────────────
log "INFO" "Creating compressed archive: ${ARCHIVE}"
tar -czf "${ARCHIVE}" -C "$(dirname "${BACKUP_DIR}")" "${BACKUP_NAME}"
log "INFO" "Archive created: $(du -h "${ARCHIVE}" | cut -f1)"

# ── Optional GPG encryption ─────────────────────────────────────────────────
UPLOAD_FILE="${ARCHIVE}"
if [[ "${ENCRYPT}" == "--encrypt" ]]; then
    if ! command -v gpg &> /dev/null; then
        log "ERROR" "gpg not found — cannot encrypt"
        exit 1
    fi
    log "INFO" "Encrypting with GPG (recipient: ${GPG_RECIPIENT})..."
    gpg --batch --yes --trust-model always \
        --recipient "${GPG_RECIPIENT}" \
        --output "${ARCHIVE}.gpg" \
        --encrypt "${ARCHIVE}"
    UPLOAD_FILE="${ARCHIVE}.gpg"
    log "INFO" "Encrypted archive: $(du -h "${UPLOAD_FILE}" | cut -f1)"
fi

# ── Upload ───────────────────────────────────────────────────────────────────
REMOTE_KEY="${S3_BUCKET}/${BACKUP_NAME}/$(basename "${UPLOAD_FILE}")"
log "INFO" "Uploading to ${REMOTE_KEY}..."

if ${USE_MC}; then
    # Configure mc alias if using custom endpoint
    if [[ -n "${S3_ENDPOINT}" ]]; then
        mc alias set s3backup "${S3_ENDPOINT}" "${AWS_ACCESS_KEY_ID:-}" "${AWS_SECRET_ACCESS_KEY:-}"
    fi
    mc cp "${UPLOAD_FILE}" "${REMOTE_KEY}"
else
    aws s3 cp "${UPLOAD_FILE}" "${REMOTE_KEY}" "${S3_ARGS[@]}"
fi

log "INFO" "Upload complete"

# ── Upload checksum for remote verification ──────────────────────────────────
CHECKSUM=$(sha256sum "${UPLOAD_FILE}" | awk '{print $1}')
echo "${CHECKSUM}  $(basename "${UPLOAD_FILE}")" > "${UPLOAD_FILE}.sha256"

if ${USE_MC}; then
    mc cp "${UPLOAD_FILE}.sha256" "${S3_BUCKET}/${BACKUP_NAME}/$(basename "${UPLOAD_FILE}").sha256"
else
    aws s3 cp "${UPLOAD_FILE}.sha256" "${S3_BUCKET}/${BACKUP_NAME}/$(basename "${UPLOAD_FILE}").sha256" "${S3_ARGS[@]}"
fi

log "INFO" "Checksum uploaded: ${CHECKSUM}"

# ── Cleanup local archive ───────────────────────────────────────────────────
rm -f "${ARCHIVE}" "${ARCHIVE}.gpg" "${UPLOAD_FILE}.sha256"
log "INFO" "Local archive cleaned up"
log "INFO" "Remote backup complete: ${REMOTE_KEY}"