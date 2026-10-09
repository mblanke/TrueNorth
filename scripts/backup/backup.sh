#!/usr/bin/env bash
# =============================================================================
# TrueNorth Range — platform backup
# =============================================================================
# Produces BACKUP_DIR/truenorth-backup-<UTC timestamp>/ containing:
#
#   postgres/globals.sql        roles (pg_dumpall --globals-only)
#   postgres/<db>.dump          pg_dump -Fc of EVERY non-template database on
#                               the server (app, keycloak, lrs, ... discovered)
#   minio/<bucket>/...          mc mirror of every bucket
#   secrets/env.enc + env.key.enc
#                               the env file (holds TN_SECRETS_KEY and every
#                               credential a restore needs), AES-256 encrypted to
#                               the escrow RSA public key. Never stored in plain text.
#   secrets/secrets.tar.enc     SECRETS_DIR (the installer's config/secrets/: every
#                               persisted secret, one file each), tarred and encrypted
#                               with the same data key. restore-secrets.sh writes them
#                               back before the installer is re-run on a rebuilt host.
#   manifest.json, SHA256SUMS
#
# Not backed up, on purpose (docs/runbooks/backup-restore.md):
#   Redis      — Celery broker, cache and rate-limit counters. Restoring a stale
#                queue would replay provisioning tasks.
#   OpenSearch — telemetry is not copied into the backup directory. When
#                OPENSEARCH_SNAPSHOT_REPO is set (the installer sets tn_snapshots, a
#                filesystem repository at $TN_DATA_ROOT/opensearch-snapshots that
#                70-telemetry registers) a snapshot is taken there and named in the
#                manifest; otherwise the manifest records "not backed up".
#
# Usage: backup.sh            (all settings from the environment)
#
# Environment (see also lib.sh):
#   BACKUP_DIR               default /srv/truenorth/backups
#   BACKUP_RETENTION_DAYS    prune complete backups older than N days; 0 = never.
#                            Falls back to RETENTION_DAYS, default 30. The newest
#                            backup is never pruned.
#   BACKUP_ESCROW_PUBKEY     RSA public key (PEM) the env file is encrypted to.
#   BACKUP_ESCROW            set to "out-of-band" to attest that TN_SECRETS_KEY
#                            is escrowed elsewhere (then no key file is needed).
#   SECRETS_DIR              the installer's persisted secrets (config/secrets/);
#                            escrowed with the env file when set. Unset: env file only.
#   BACKUP_MIN_FREE_GB       refuse to start with less free on BACKUP_DIR's disk, or
#                            less than the previous backup's size (default 10).
#   BACKUP_MAX_TOTAL_GB      after a backup, prune the oldest (never the newest) while
#                            everything under BACKUP_ROOT exceeds this; 0 = no cap.
#   BACKUP_ROOT              where that cap is measured (default BACKUP_DIR; the cron
#                            wrapper and the installer's pre-upgrade backup pass the
#                            parent of their daily/, weekly/, pre-upgrade/ directories).
#   OPENSEARCH_SNAPSHOT_REPO optional, see above. OPENSEARCH_URL (inside the
#                            opensearch container, default https://localhost:9200),
#                            OPENSEARCH_SNAPSHOT_AUTH (user:pass, read from the env
#                            file) and OPENSEARCH_SNAPSHOT_CACERT (the internal CA, path
#                            inside the container; default config/certs/ca.pem). The
#                            certificate is verified: no `curl -k`. A snapshot that is
#                            not SUCCESS fails the backup.
#   OPENSEARCH_SNAPSHOT_KEEP keep the newest N tn-* snapshots in the repository and
#                            delete older ones after a successful snapshot (default 14;
#                            0 = keep all).
#   OPENSSL                  default: openssl
#
# Exit codes: 0 ok; 1 failed (no backup kept); 3 data backed up but secrets NOT
# escrowed (configure BACKUP_ESCROW_PUBKEY or BACKUP_ESCROW=out-of-band).
# Every failure also raises an alert (stderr + syslog, webhook if configured).
# =============================================================================
set -euo pipefail
IFS=$'\n\t'
umask 077

# shellcheck source=scripts/backup/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

BACKUP_DIR="${BACKUP_DIR:-/srv/truenorth/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-${RETENTION_DAYS:-30}}"
BACKUP_ESCROW_PUBKEY="${BACKUP_ESCROW_PUBKEY:-}"
BACKUP_ESCROW="${BACKUP_ESCROW:-}"
SECRETS_DIR="${SECRETS_DIR:-}"
BACKUP_MIN_FREE_GB="${BACKUP_MIN_FREE_GB:-10}"
BACKUP_MAX_TOTAL_GB="${BACKUP_MAX_TOTAL_GB:-0}"
BACKUP_ROOT="${BACKUP_ROOT:-${BACKUP_DIR}}"
OPENSEARCH_SNAPSHOT_REPO="${OPENSEARCH_SNAPSHOT_REPO:-}"
OPENSEARCH_SNAPSHOT_KEEP="${OPENSEARCH_SNAPSHOT_KEEP:-14}"
OPENSSL="${OPENSSL:-openssl}"

TIMESTAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
BACKUP_NAME="truenorth-backup-${TIMESTAMP}"
FINAL_PATH="${BACKUP_DIR}/${BACKUP_NAME}"
WORK_PATH="${BACKUP_DIR}/.partial-${BACKUP_NAME}"
LOCK_DIR="${BACKUP_DIR}/.lock"
CURRENT_STEP="setup"
EXIT_CODE=0
LOCKED=0

on_exit() {
    local rc=$?
    if (( rc != 0 )) && [[ "${CURRENT_STEP}" != "done" ]]; then
        alert "backup FAILED at step '${CURRENT_STEP}' (exit ${rc}); partial output removed"
        rm -rf "${WORK_PATH}"
    fi
    if (( LOCKED )); then
        rmdir "${LOCK_DIR}" 2>/dev/null || true
    fi
}
trap on_exit EXIT

compose_init
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
# One backup at a time (mkdir is atomic; flock is not on every platform).
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
    CURRENT_STEP="lock"
    die "another backup holds ${LOCK_DIR}; remove it if no backup is running"
fi
LOCKED=1

# ── 0. Room for it ───────────────────────────────────────────────────────────
# A backup that fills the disk takes the database down with it. Need the floor, and at
# least as much as the previous backup took.
CURRENT_STEP="disk"
[[ "${BACKUP_MIN_FREE_GB}" =~ ^[0-9]+$ ]] || die "BACKUP_MIN_FREE_GB must be a whole number of GB"
FREE_KB="$(free_kb "${BACKUP_DIR}")"
LAST_BACKUP="$(all_backups "${BACKUP_ROOT}" | tail -n 1)"
NEED_KB=$(( BACKUP_MIN_FREE_GB * 1024 * 1024 ))
if [[ -n "${LAST_BACKUP}" ]]; then
    LAST_KB="$(size_kb "${LAST_BACKUP}")"
    (( LAST_KB > NEED_KB )) && NEED_KB="${LAST_KB}"
fi
if (( FREE_KB < NEED_KB )); then
    die "only $(( FREE_KB / 1024 )) MB free on ${BACKUP_DIR}; need $(( NEED_KB / 1024 )) MB (BACKUP_MIN_FREE_GB=${BACKUP_MIN_FREE_GB}, or the previous backup's size). Free space or lower BACKUP_MAX_TOTAL_GB."
fi

mkdir -p "${WORK_PATH}/postgres" "${WORK_PATH}/minio" "${WORK_PATH}/secrets"
log "INFO" "Backup ${BACKUP_NAME} → ${BACKUP_DIR}"

PG_USER="$(env_get POSTGRES_USER)"
PG_DB="$(env_get POSTGRES_DB)"
[[ -n "${PG_USER}" && -n "${PG_DB}" ]] || die "POSTGRES_USER / POSTGRES_DB missing from ${ENV_FILE}"

# ── 1. PostgreSQL: globals + every database ──────────────────────────────────
CURRENT_STEP="postgres"
service_cid postgres >/dev/null
dc exec -T postgres pg_dumpall -U "${PG_USER}" --globals-only >"${WORK_PATH}/postgres/globals.sql"

# (A read loop, not mapfile: the scripts also run under macOS's bash 3.2.)
DATABASES=()
while IFS= read -r db; do
    [[ -n "$db" ]] && DATABASES+=("$db")
done < <(dc exec -T postgres psql -U "${PG_USER}" -d "${PG_DB}" -v ON_ERROR_STOP=1 -Atc \
    "select datname from pg_database where not datistemplate and datallowconn order by 1" | tr -d '\r')
(( ${#DATABASES[@]} > 0 )) || die "no databases found on the postgres server"

DB_JSON=""
for db in "${DATABASES[@]}"; do
    [[ -n "$db" ]] || continue
    CURRENT_STEP="postgres:${db}"
    out="${WORK_PATH}/postgres/${db}.dump"
    dc exec -T postgres pg_dump -U "${PG_USER}" -Fc -d "${db}" >"${out}" </dev/null
    # A dump pg_restore cannot list is not a backup.
    dc exec -T postgres pg_restore --list <"${out}" >/dev/null
    size="$(wc -c <"${out}" | tr -d ' ')"
    log "INFO" "  postgres ${db}: ${size} bytes"
    DB_JSON+="${DB_JSON:+,}{\"name\":$(json_str "$db"),\"file\":$(json_str "postgres/${db}.dump"),\"bytes\":${size}}"
done

# ── 2. MinIO: every bucket ───────────────────────────────────────────────────
CURRENT_STEP="minio"
mc_run "${WORK_PATH}/minio" mirror --quiet --preserve tn /backup >/dev/null </dev/null
# mirror writes no directory for an empty bucket; record every bucket explicitly
# so restore re-creates it.
while IFS= read -r bucket; do
    bucket="${bucket%/}"
    [[ -n "$bucket" && "$bucket" != */* && "$bucket" != .* ]] || continue
    mkdir -p "${WORK_PATH}/minio/${bucket}"
done < <(mc_run "${WORK_PATH}/minio" ls tn </dev/null | awk '{print $NF}')
SRC_OBJECTS="$(mc_run "${WORK_PATH}/minio" ls --recursive tn | grep -c . || true)"
BAK_OBJECTS="$(find "${WORK_PATH}/minio" -type f | grep -c . || true)"
BUCKETS="$(find "${WORK_PATH}/minio" -mindepth 1 -maxdepth 1 -type d | grep -c . || true)"
if (( BAK_OBJECTS < SRC_OBJECTS )); then
    die "MinIO mirror incomplete: ${BAK_OBJECTS} files for ${SRC_OBJECTS} objects"
fi
log "INFO" "  minio: ${BUCKETS} buckets, ${BAK_OBJECTS} objects"

# ── 3. OpenSearch (optional) ─────────────────────────────────────────────────
CURRENT_STEP="opensearch"
if [[ -n "${OPENSEARCH_SNAPSHOT_REPO}" ]]; then
    [[ "${OPENSEARCH_SNAPSHOT_KEEP}" =~ ^[0-9]+$ ]] || die "OPENSEARCH_SNAPSHOT_KEEP must be a whole number"
    snap="tn-$(printf '%s' "${TIMESTAMP}" | tr '[:upper:]' '[:lower:]')"
    # os_curl (lib.sh): inside the container, CA-verified, credentials on stdin.
    # The repository's own security/system indices are left out: a restore never replaces them.
    os_curl -X PUT -H 'Content-Type: application/json' \
        "${OPENSEARCH_URL}/_snapshot/${OPENSEARCH_SNAPSHOT_REPO}/${snap}?wait_for_completion=true" \
        -d '{"indices":"*,-.*","include_global_state":false}' >"${WORK_PATH}/.os-snapshot.json"
    # wait_for_completion answers 200 for a PARTIAL or FAILED snapshot too.
    grep -q '"state":"SUCCESS"' "${WORK_PATH}/.os-snapshot.json" \
        || die "OpenSearch snapshot ${snap} did not succeed: $(head -c 400 "${WORK_PATH}/.os-snapshot.json")"
    rm -f "${WORK_PATH}/.os-snapshot.json"
    OS_JSON="{\"status\":\"snapshot\",\"repository\":$(json_str "${OPENSEARCH_SNAPSHOT_REPO}"),\"snapshot\":$(json_str "$snap")}"
    log "INFO" "  opensearch: snapshot ${snap} in repository ${OPENSEARCH_SNAPSHOT_REPO}"
    # Each snapshot pins the indices it holds, so the repository would otherwise keep every
    # telemetry index ever written. Names sort by time (tn-<UTC timestamp>); only ours are pruned.
    if (( OPENSEARCH_SNAPSHOT_KEEP > 0 )); then
        CURRENT_STEP="opensearch:prune"
        ours="$(os_curl "${OPENSEARCH_URL}/_cat/snapshots/${OPENSEARCH_SNAPSHOT_REPO}?h=id" \
            | tr -d '\r ' | grep -E '^tn-[0-9]{8}t[0-9]{6}z$' | sort || true)"
        n_ours="$(printf '%s\n' "${ours}" | grep -c . || true)"
        old_snaps=""
        if (( n_ours > OPENSEARCH_SNAPSHOT_KEEP )); then
            old_snaps="$(printf '%s\n' "${ours}" | head -n "$(( n_ours - OPENSEARCH_SNAPSHOT_KEEP ))")"
        fi
        for old in ${old_snaps}; do
            [[ "$old" == "$snap" ]] && continue
            log "INFO" "  opensearch: deleting snapshot ${old} (keeping the newest ${OPENSEARCH_SNAPSHOT_KEEP})"
            os_curl -X DELETE "${OPENSEARCH_URL}/_snapshot/${OPENSEARCH_SNAPSHOT_REPO}/${old}" >/dev/null
        done
    fi
else
    OS_JSON='{"status":"not-backed-up","reason":"no OPENSEARCH_SNAPSHOT_REPO registered; telemetry is not part of this backup"}'
    log "WARN" "  opensearch: not backed up (OPENSEARCH_SNAPSHOT_REPO unset)"
fi

# ── 4. Secrets escrow (TN_SECRETS_KEY and every credential) ──────────────────
CURRENT_STEP="escrow"
if [[ -n "${BACKUP_ESCROW_PUBKEY}" ]]; then
    [[ -r "${BACKUP_ESCROW_PUBKEY}" ]] || die "BACKUP_ESCROW_PUBKEY not readable: ${BACKUP_ESCROW_PUBKEY}"
    # Hybrid: a fresh 256-bit data key encrypts the env file; the escrow RSA key
    # wraps the data key. Neither the env file nor the data key touches disk in clear.
    TN_ESCROW_DATA_KEY="$("${OPENSSL}" rand -hex 32)"
    export TN_ESCROW_DATA_KEY
    "${OPENSSL}" enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt \
        -pass env:TN_ESCROW_DATA_KEY -in "${ENV_FILE}" -out "${WORK_PATH}/secrets/env.enc"
    ESCROW_FILES='"secrets/env.enc","secrets/env.key.enc"'
    # The persisted secrets, one file each (the installer's config/secrets/): tarred
    # straight into the cipher, never written to disk in clear.
    if [[ -n "${SECRETS_DIR}" ]]; then
        [[ -d "${SECRETS_DIR}" && -r "${SECRETS_DIR}" ]] || die "SECRETS_DIR not a readable directory: ${SECRETS_DIR}"
        n_secrets="$(find "${SECRETS_DIR}" -mindepth 1 -maxdepth 1 -type f | grep -c . || true)"
        (( n_secrets > 0 )) || die "SECRETS_DIR ${SECRETS_DIR} holds no secrets"
        tar -C "${SECRETS_DIR}" -cf - . \
            | "${OPENSSL}" enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt \
                -pass env:TN_ESCROW_DATA_KEY -out "${WORK_PATH}/secrets/secrets.tar.enc"
        ESCROW_FILES+=',"secrets/secrets.tar.enc"'
        log "INFO" "  escrow: ${n_secrets} persisted secrets from ${SECRETS_DIR}"
    fi
    printf '%s' "${TN_ESCROW_DATA_KEY}" | "${OPENSSL}" pkeyutl -encrypt -pubin \
        -inkey "${BACKUP_ESCROW_PUBKEY}" -pkeyopt rsa_padding_mode:oaep \
        -pkeyopt rsa_oaep_md:sha256 -out "${WORK_PATH}/secrets/env.key.enc"
    unset TN_ESCROW_DATA_KEY
    fp="$("${OPENSSL}" pkey -pubin -in "${BACKUP_ESCROW_PUBKEY}" -outform DER | "${OPENSSL}" dgst -sha256 -r | cut -d' ' -f1)"
    ESCROW_JSON="{\"status\":\"encrypted\",\"files\":[${ESCROW_FILES}],\"recipient_sha256\":\"${fp}\"}"
    if [[ -z "$(env_get TN_SECRETS_KEY)" ]]; then
        log "WARN" "  escrow: ${ENV_FILE} has no TN_SECRETS_KEY (fine before PR #90 lands)"
    fi
    log "INFO" "  escrow: env file encrypted to key ${fp:0:16}…"
elif [[ "${BACKUP_ESCROW}" == "out-of-band" ]]; then
    rmdir "${WORK_PATH}/secrets"
    ESCROW_JSON='{"status":"out-of-band","note":"operator attests TN_SECRETS_KEY and the env file are escrowed outside this backup"}'
    log "INFO" "  escrow: out-of-band (BACKUP_ESCROW=out-of-band)"
else
    rmdir "${WORK_PATH}/secrets"
    ESCROW_JSON='{"status":"missing"}'
    EXIT_CODE=3
    log "WARN" "  escrow: NOT escrowed — set BACKUP_ESCROW_PUBKEY or BACKUP_ESCROW=out-of-band"
fi

# ── 5. Manifest + checksums ──────────────────────────────────────────────────
CURRENT_STEP="manifest"
GIT_REV="$(git -C "${TN_BACKUP_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
PROJECT="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$(service_cid postgres)")"
PG_VERSION="$(dc exec -T postgres psql -U "${PG_USER}" -d "${PG_DB}" -Atc 'show server_version' | tr -d '\r')"
cat >"${WORK_PATH}/manifest.json" <<EOF
{
  "format": "truenorth-backup/2",
  "name": $(json_str "${BACKUP_NAME}"),
  "created_utc": $(json_str "${TIMESTAMP}"),
  "host": $(json_str "$(hostname)"),
  "compose_project": $(json_str "${PROJECT}"),
  "source_git_rev": $(json_str "${GIT_REV}"),
  "postgres": {"server_version": $(json_str "${PG_VERSION}"), "globals": "postgres/globals.sql", "databases": [${DB_JSON}]},
  "minio": {"buckets": ${BUCKETS}, "objects": ${BAK_OBJECTS}, "dir": "minio"},
  "opensearch": ${OS_JSON},
  "redis": {"status": "not-backed-up", "reason": "transient broker/cache; restoring would replay tasks"},
  "escrow": ${ESCROW_JSON}
}
EOF
(cd "${WORK_PATH}" && sha256_file_list >SHA256SUMS)
(cd "${WORK_PATH}" && sha256_check)

CURRENT_STEP="finalize"
mv "${WORK_PATH}" "${FINAL_PATH}"

# ── 6. Retention (only after a successful backup) ────────────────────────────
CURRENT_STEP="retention"
if [[ "${BACKUP_RETENTION_DAYS}" =~ ^[0-9]+$ ]] && (( BACKUP_RETENTION_DAYS > 0 )); then
    while IFS= read -r old; do
        [[ "$old" == "${FINAL_PATH}" ]] && continue
        log "INFO" "  pruning $(basename "$old") (older than ${BACKUP_RETENTION_DAYS}d)"
        rm -rf "$old"
    done < <(find "${BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -name 'truenorth-backup-*' \
                -mtime "+${BACKUP_RETENTION_DAYS}")
fi
# Leftovers of runs that were killed hard (SIGKILL skips the trap).
find "${BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -name '.partial-*' -mtime +1 -exec rm -rf {} +
# The size cap, across every backup under BACKUP_ROOT. Never this one.
prune_to_size "${BACKUP_ROOT}" "${BACKUP_MAX_TOTAL_GB}" "${FINAL_PATH}"

CURRENT_STEP="done"
log "INFO" "Backup complete: ${FINAL_PATH} ($(du -sh "${FINAL_PATH}" | cut -f1))"
# Exit only on the non-zero path. An unconditional `exit` as the last statement makes
# ShellCheck 0.9-0.11 report the EXIT-trap handler above as unreachable (SC2317/SC2329).
if (( EXIT_CODE == 3 )); then
    alert "backup ${BACKUP_NAME} completed WITHOUT secrets escrow; TN_SECRETS_KEY is not recoverable from it"
    exit 3
fi
