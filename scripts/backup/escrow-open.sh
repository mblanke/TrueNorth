#!/usr/bin/env bash
# =============================================================================
# TrueNorth Range — open the secrets escrow of a backup.sh backup
# =============================================================================
# Usage: escrow-open.sh <backup-dir> <escrow-private-key.pem> [output-file]
#
# Decrypts secrets/env.enc (the stack's env file, incl. TN_SECRETS_KEY) with the
# escrow RSA private key. Writes to output-file (mode 600) or stdout. Run it on a
# trusted host; the private key should never live on the backup host.
# =============================================================================
set -euo pipefail
umask 077

BACKUP_PATH="${1:?Usage: $0 <backup-dir> <escrow-private-key.pem> [output-file]}"
PRIVKEY="${2:?Usage: $0 <backup-dir> <escrow-private-key.pem> [output-file]}"
OUT="${3:-}"
OPENSSL="${OPENSSL:-openssl}"

for f in "${BACKUP_PATH}/secrets/env.enc" "${BACKUP_PATH}/secrets/env.key.enc" "${PRIVKEY}"; do
    [[ -r "$f" ]] || { echo "not readable: $f" >&2; exit 1; }
done

TN_ESCROW_DATA_KEY="$("${OPENSSL}" pkeyutl -decrypt -inkey "${PRIVKEY}" \
    -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 \
    -in "${BACKUP_PATH}/secrets/env.key.enc")"
export TN_ESCROW_DATA_KEY

args=(enc -d -aes-256-cbc -pbkdf2 -iter 200000 -md sha256
      -pass env:TN_ESCROW_DATA_KEY -in "${BACKUP_PATH}/secrets/env.enc")
if [[ -n "${OUT}" ]]; then
    "${OPENSSL}" "${args[@]}" -out "${OUT}"
    echo "wrote ${OUT}" >&2
else
    "${OPENSSL}" "${args[@]}"
fi
