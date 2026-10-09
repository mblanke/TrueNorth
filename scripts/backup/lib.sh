#!/usr/bin/env bash
# =============================================================================
# TrueNorth Range — shared helpers for scripts/backup/{backup,restore,drill}.sh
# =============================================================================
# Sourced, never executed. Everything talks to the stack the way the installer
# does: `docker compose -f <files> --env-file <env> ...`, so it targets the same
# compose project (and therefore the same containers) the operator runs, without
# assuming a container_name. Credentials are read from the env file, never
# hardcoded and never passed on a command line.
#
# Inputs (environment):
#   COMPOSE_FILE   colon-separated compose files (docker's own convention).
#                  Default: infra/platform/docker/compose.prod.yml
#   ENV_FILE       the stack's env file.
#                  Default: infra/platform/docker/.env.production
#   COMPOSE_PROJECT_NAME  optional; docker compose honours it as usual.
#   MC_IMAGE       image that provides `mc`. Default: the stack's pinned MinIO
#                  image, which ships mc of the same release.
#   BACKUP_WEBHOOK_URL (or WEBHOOK_URL)  optional extra alert channel.
#   OPENSEARCH_URL / OPENSEARCH_SNAPSHOT_CACERT  how os_curl reaches the node from inside
#                  the opensearch container (https://localhost:9200, config/certs/ca.pem).
# shellcheck shell=bash

TN_BACKUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${TN_BACKUP_ROOT}/infra/platform/docker/compose.prod.yml}"
ENV_FILE="${ENV_FILE:-${TN_BACKUP_ROOT}/infra/platform/docker/.env.production}"
MC_IMAGE="${MC_IMAGE:-pgsty/minio:RELEASE.2026-08-04T00-00-00Z}"
BACKUP_WEBHOOK_URL="${BACKUP_WEBHOOK_URL:-${WEBHOOK_URL:-}}"
TN_LOG_TAG="${TN_LOG_TAG:-truenorth-backup}"
OPENSEARCH_URL="${OPENSEARCH_URL:-https://localhost:9200}"
OPENSEARCH_SNAPSHOT_CACERT="${OPENSEARCH_SNAPSHOT_CACERT:-config/certs/ca.pem}"

log() {
    local level="$1"; shift
    printf '[%s] [%s] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$level" "$*"
}

die() {
    log "ERROR" "$*" >&2
    exit 1
}

# Failure alert. Always stderr (cron mails it / the log captures it) and syslog
# when `logger` exists; the webhook is an optional extra, not the only channel.
alert() {
    local msg="$1"
    printf '[%s] [ALERT] %s: %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "${TN_LOG_TAG}" "$msg" >&2
    if command -v logger >/dev/null 2>&1; then
        logger -p user.err -t "${TN_LOG_TAG}" -- "$msg" 2>/dev/null || true
    fi
    if [[ -n "${BACKUP_WEBHOOK_URL}" ]] && command -v curl >/dev/null 2>&1; then
        local host payload
        host="$(hostname)"
        # Minimal JSON escaping for the two fields we interpolate.
        msg="${msg//\\/\\\\}"; msg="${msg//\"/\\\"}"
        payload=$(printf '{"text":"%s on %s: %s"}' "${TN_LOG_TAG}" "${host}" "${msg}")
        curl -sf --max-time 10 -X POST -H "Content-Type: application/json" \
            -d "${payload}" "${BACKUP_WEBHOOK_URL}" >/dev/null 2>&1 \
            || log "WARN" "alert webhook delivery failed" >&2
    fi
}

# Resolve a possibly-relative path: as given if it exists, else under the repo root.
_resolve() {
    local p="$1"
    if [[ "$p" == /* || -e "$p" ]]; then
        printf '%s' "$p"
    else
        printf '%s/%s' "${TN_BACKUP_ROOT}" "$p"
    fi
}

# Builds TN_COMPOSE_ARGS from COMPOSE_FILE / ENV_FILE and checks they exist.
compose_init() {
    local f
    local -a _files
    TN_COMPOSE_ARGS=()
    IFS=':' read -r -a _files <<<"${COMPOSE_FILE}"
    for f in "${_files[@]}"; do
        [[ -n "$f" ]] || continue
        f="$(_resolve "$f")"
        [[ -f "$f" ]] || die "compose file not found: $f (set COMPOSE_FILE)"
        TN_COMPOSE_ARGS+=(-f "$f")
    done
    ENV_FILE="$(_resolve "${ENV_FILE}")"
    [[ -r "${ENV_FILE}" ]] || die "env file not readable: ${ENV_FILE} (set ENV_FILE)"
    TN_COMPOSE_ARGS+=(--env-file "${ENV_FILE}")
}

dc() {
    docker compose "${TN_COMPOSE_ARGS[@]}" "$@"
}

# Moodle (optional; install/roles/tn_moodle): its own compose project,
# MOODLE_COMPOSE_FILE (compose.moodle-prod.yml) with MOODLE_ENV_FILE. Its database is in
# the platform's PostgreSQL, so dc's dumps hold it; moodle_dc reaches its files.
# moodle_state prints: off (MOODLE_COMPOSE_FILE unset), absent (set, but no env file yet:
# the node is not installed), or on.
moodle_state() {
    if [[ -z "${MOODLE_COMPOSE_FILE:-}" ]]; then
        echo off
    elif [[ -n "${MOODLE_ENV_FILE:-}" && -r "${MOODLE_ENV_FILE}" ]]; then
        echo on
    else
        echo absent
    fi
}

moodle_dc() {
    local f
    f="$(_resolve "${MOODLE_COMPOSE_FILE}")"
    [[ -f "$f" ]] || die "Moodle compose file not found: $f (MOODLE_COMPOSE_FILE)"
    docker compose -f "$f" --env-file "${MOODLE_ENV_FILE}" "$@"
}

# The moodledata paths a backup leaves out: Moodle rebuilds them (caches, sessions, temp).
# shellcheck disable=SC2034 # used by backup.sh, which sources this file
MOODLE_DATA_EXCLUDES=(--exclude=./cache --exclude=./localcache --exclude=./sessions
    --exclude=./temp --exclude=./trashdir --exclude=./lock)

# env_get KEY [default] — the last KEY=value in ENV_FILE, quotes stripped.
# The file is parsed, not sourced: nothing in it is executed.
env_get() {
    local key="$1" def="${2:-}" line val
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "${ENV_FILE}" | tail -n 1 || true)"
    if [[ -z "$line" ]]; then
        printf '%s' "$def"
        return 0
    fi
    val="${line#*=}"
    val="${val%$'\r'}"
    if [[ "$val" =~ ^\"(.*)\"$ || "$val" =~ ^\'(.*)\'$ ]]; then
        val="${BASH_REMATCH[1]}"
    fi
    if [[ -z "$val" ]]; then
        val="$def"
    fi
    printf '%s' "$val"
}

# Container id of a running service, or fail.
service_cid() {
    local svc="$1" cid
    cid="$(dc ps -q "$svc" 2>/dev/null | head -n 1)"
    [[ -n "$cid" ]] || die "service '$svc' is not running in this compose project"
    printf '%s' "$cid"
}

# First network the given container is attached to (the MinIO backend network).
container_network() {
    docker inspect --format '{{range $k, $v := .NetworkSettings.Networks}}{{println $k}}{{end}}' "$1" \
        | sed '/^$/d' | head -n 1
}

# mc_run <host-dir> <mc args...>
# Runs mc in a throwaway container on MinIO's network with <host-dir> mounted at
# /backup. Credentials travel as inherited environment variables (`-e NAME`),
# so they never appear in a process listing.
mc_run() {
    local hostdir="$1"; shift
    local cid net
    cid="$(service_cid minio)"
    net="$(container_network "$cid")"
    [[ -n "$net" ]] || die "cannot determine the MinIO container's network"
    TN_MC_USER="$(env_get MINIO_ACCESS_KEY)" \
    TN_MC_PASS="$(env_get MINIO_SECRET_KEY)" \
    docker run --rm --network "$net" \
        --user "$(id -u):$(id -g)" \
        -e TN_MC_USER -e TN_MC_PASS -e HOME=/tmp \
        -v "${hostdir}:/backup" \
        --entrypoint /bin/sh "${MC_IMAGE}" -c '
            set -eu
            mc alias set tn http://minio:9000 "$TN_MC_USER" "$TN_MC_PASS" >/dev/null
            exec mc "$@"' mc "$@"
}

# os_curl <curl args...> — curl run inside the opensearch container against
# OPENSEARCH_URL<path>. The node certificate is verified against the internal CA (no
# `curl -k`); OPENSEARCH_SNAPSHOT_AUTH (user:pass, read from the env file) reaches curl
# in a netrc-style config on stdin (-K -), never on a command line. Fails on HTTP >= 400.
os_curl() {
    local auth cfg=""
    local -a opts=(-sS --fail --max-time 3600)
    auth="$(env_get OPENSEARCH_SNAPSHOT_AUTH)"
    if [[ "${OPENSEARCH_URL}" == https:* ]]; then opts+=(--cacert "${OPENSEARCH_SNAPSHOT_CACERT}"); fi
    if [[ -n "$auth" ]]; then
        cfg="user = \"${auth//\"/\\\"}\""
    fi
    printf '%s\n' "${cfg}" | dc exec -T opensearch curl "${opts[@]}" -K - "$@"
}

sha256_file_list() {
    # Prints "<sha256>  <path>" for every file under the cwd except SHA256SUMS.
    if command -v sha256sum >/dev/null 2>&1; then
        find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum
    else
        find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 shasum -a 256
    fi
}

sha256_check() {
    # Verifies SHA256SUMS in the cwd.
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum --quiet -c SHA256SUMS
    else
        shasum -a 256 --quiet -c SHA256SUMS
    fi
}

# free_kb <dir> — kilobytes available to an unprivileged writer on <dir>'s filesystem.
free_kb() {
    df -Pk "$1" | awk 'NR == 2 { print $4 }'
}

# size_kb <path> — kilobytes used under <path> (0 if missing).
size_kb() {
    if [[ -e "$1" ]]; then du -sk "$1" | awk '{ print $1 }'; else echo 0; fi
}

# all_backups <root> — every complete backup under <root> (at most one level of type
# directories: daily/, weekly/, pre-upgrade/ ...), oldest first by its UTC timestamp name.
all_backups() {
    find "$1" -mindepth 1 -maxdepth 2 -type d -name 'truenorth-backup-*' 2>/dev/null \
        | awk -F/ '{ print $NF "\t" $0 }' | sort | cut -f2-
}

# prune_to_size <root> <max_gb> [<keep>...] — delete the oldest backups under <root> while
# all of them together exceed <max_gb> GB. Never deletes a <keep> path, nor the newest
# backup. 0 or empty = no cap.
prune_to_size() {
    local root="$1" max_gb="${2:-0}" b total newest
    shift 2 || true
    [[ "${max_gb}" =~ ^[0-9]+$ ]] && (( max_gb > 0 )) || return 0
    local max_kb=$(( max_gb * 1024 * 1024 ))
    newest="$(all_backups "$root" | tail -n 1)"
    total="$(size_kb "$root")"
    while (( total > max_kb )); do
        b=""
        while IFS= read -r cand; do
            [[ "$cand" == "$newest" ]] && continue
            local k skip=0
            for k in "$@"; do [[ "$cand" == "$k" ]] && skip=1; done
            (( skip )) && continue
            b="$cand"; break
        done < <(all_backups "$root")
        if [[ -z "$b" ]]; then
            log "WARN" "backups under ${root} use $(( total / 1024 / 1024 )) GB, over the ${max_gb} GB cap, and only the newest is left" >&2
            return 0
        fi
        log "INFO" "  pruning $(basename "$b") (backups over the ${max_gb} GB cap)"
        rm -rf "$b"
        total="$(size_kb "$root")"
    done
}

json_str() {
    local s="$1"
    s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
    printf '"%s"' "$s"
}
