#!/usr/bin/env bash
# =============================================================================
# TrueNorth Range — write a backup's escrowed secrets back onto a host
# =============================================================================
# Usage: restore-secrets.sh <backup-dir> <escrow-private-key.pem>
#            [--secrets-dir DIR] [--env-file FILE] [--force]
#
# The step BEFORE restore.sh and the installer on a rebuilt (or reinstalled) host:
# the restored databases hold the OLD passwords, TN_SECRETS_KEY sealed the stored
# credentials, and the installer must render the env file from the same values. So the
# secrets go back first, then data (restore.sh), then the installer re-runs and finds them
# (docs/runbooks/backup-restore.md "Rebuilt host").
#
#   1. verify the backup's SHA256SUMS;
#   2. unwrap the data key with the escrow private key; decrypt secrets/secrets.tar.enc
#      into a 0700 staging directory beside the target and check it holds secrets;
#   3. if --secrets-dir (default /srv/truenorth/config/secrets) already holds DIFFERENT
#      values (a reinstall generated fresh ones), stop unless --force; with --force the
#      current directory is kept as <dir>.replaced-<UTC timestamp>, never deleted;
#   4. swap the restored directory in (0700, files 0600);
#   5. with --env-file, also write the escrowed env file there (the old one is kept as
#      <file>.replaced-<timestamp>). Without it the installer re-renders the env file.
#
# Run it on the platform host as the install user, with the private key brought in for
# the occasion (and removed afterwards); it never needs to live there.
# =============================================================================
set -euo pipefail
IFS=$'\n\t'
umask 077

usage() {
    echo "Usage: $0 <backup-dir> <escrow-private-key.pem> [--secrets-dir DIR] [--env-file FILE] [--force]" >&2
    exit 2
}
[[ $# -ge 2 ]] || usage
BACKUP_PATH="$1"; PRIVKEY="$2"; shift 2
SECRETS_TARGET="/srv/truenorth/config/secrets"
ENV_TARGET=""
FORCE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --secrets-dir) SECRETS_TARGET="${2:?--secrets-dir needs a directory}"; shift ;;
        --env-file) ENV_TARGET="${2:?--env-file needs a path}"; shift ;;
        --force) FORCE=1 ;;
        *) usage ;;
    esac
    shift
done
OPENSSL="${OPENSSL:-openssl}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

die() { echo "restore-secrets: $*" >&2; exit 1; }
sha_check() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum --quiet -c SHA256SUMS; else shasum -a 256 --quiet -c SHA256SUMS; fi
}

[[ -d "${BACKUP_PATH}" ]] || die "backup directory not found: ${BACKUP_PATH}"
for f in SHA256SUMS secrets/env.enc secrets/env.key.enc secrets/secrets.tar.enc; do
    [[ -r "${BACKUP_PATH}/$f" ]] || die "${BACKUP_PATH}/$f missing: not an escrowed backup with persisted secrets (BACKUP_ESCROW_PUBKEY and SECRETS_DIR must have been set)"
done
[[ -r "${PRIVKEY}" ]] || die "escrow private key not readable: ${PRIVKEY}"
(cd "${BACKUP_PATH}" && sha_check) || die "checksum verification FAILED for ${BACKUP_PATH}"

PARENT="$(dirname "${SECRETS_TARGET}")"
mkdir -p "${PARENT}"
STAGE="$(mktemp -d "${PARENT}/.secrets-restore.XXXXXX")"
cleanup() { rm -rf "${STAGE}"; unset TN_ESCROW_DATA_KEY; }
trap cleanup EXIT

TN_ESCROW_DATA_KEY="$("${OPENSSL}" pkeyutl -decrypt -inkey "${PRIVKEY}" \
    -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 \
    -in "${BACKUP_PATH}/secrets/env.key.enc")" || die "cannot unwrap the data key: wrong escrow private key?"
export TN_ESCROW_DATA_KEY

mkdir -p "${STAGE}/secrets"
"${OPENSSL}" enc -d -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 \
    -pass env:TN_ESCROW_DATA_KEY -in "${BACKUP_PATH}/secrets/secrets.tar.enc" \
    | tar -C "${STAGE}/secrets" -xf - || die "cannot decrypt secrets/secrets.tar.enc"
n="$(find "${STAGE}/secrets" -mindepth 1 -maxdepth 1 -type f | grep -c . || true)"
(( n > 0 )) || die "the escrow holds no secrets"
chmod 700 "${STAGE}/secrets"
find "${STAGE}/secrets" -type f -exec chmod 600 {} +

if [[ -d "${SECRETS_TARGET}" ]] && [[ -n "$(ls -A "${SECRETS_TARGET}")" ]]; then
    if diff -rq "${STAGE}/secrets" "${SECRETS_TARGET}" >/dev/null 2>&1; then
        echo "restore-secrets: ${SECRETS_TARGET} already holds exactly these ${n} secrets; nothing to do" >&2
    elif (( ! FORCE )); then
        die "${SECRETS_TARGET} holds different secrets (a reinstall generates fresh ones). Re-run with --force to replace them; the current ones are kept as ${SECRETS_TARGET}.replaced-<timestamp>."
    else
        mv "${SECRETS_TARGET}" "${SECRETS_TARGET}.replaced-${STAMP}"
        echo "restore-secrets: kept the replaced secrets as ${SECRETS_TARGET}.replaced-${STAMP}" >&2
    fi
fi
if [[ ! -e "${SECRETS_TARGET}" ]] || [[ -z "$(ls -A "${SECRETS_TARGET}")" ]]; then
    [[ -d "${SECRETS_TARGET}" ]] && rmdir "${SECRETS_TARGET}"
    mv "${STAGE}/secrets" "${SECRETS_TARGET}"   # the target does not exist here: a rename
    echo "restore-secrets: wrote ${n} secrets to ${SECRETS_TARGET}" >&2
fi

if [[ -n "${ENV_TARGET}" ]]; then
    tmp="$(mktemp "$(dirname "${ENV_TARGET}")/.env-restore.XXXXXX")"
    "${OPENSSL}" enc -d -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 \
        -pass env:TN_ESCROW_DATA_KEY -in "${BACKUP_PATH}/secrets/env.enc" -out "${tmp}" \
        || { rm -f "${tmp}"; die "cannot decrypt secrets/env.enc"; }
    chmod 600 "${tmp}"
    if [[ -e "${ENV_TARGET}" ]] && ! cmp -s "${tmp}" "${ENV_TARGET}"; then
        cp -p "${ENV_TARGET}" "${ENV_TARGET}.replaced-${STAMP}"
    fi
    mv -f "${tmp}" "${ENV_TARGET}"
    echo "restore-secrets: wrote the escrowed env file to ${ENV_TARGET}" >&2
fi
echo "restore-secrets: next, restore the data (restore.sh), then re-run the installer" >&2
