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
# shellcheck shell=bash

TN_BACKUP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${TN_BACKUP_ROOT}/infra/platform/docker/compose.prod.yml}"
ENV_FILE="${ENV_FILE:-${TN_BACKUP_ROOT}/infra/platform/docker/.env.production}"
MC_IMAGE="${MC_IMAGE:-pgsty/minio:RELEASE.2026-08-04T00-00-00Z}"
BACKUP_WEBHOOK_URL="${BACKUP_WEBHOOK_URL:-${WEBHOOK_URL:-}}"
TN_LOG_TAG="${TN_LOG_TAG:-truenorth-backup}"

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

json_str() {
    local s="$1"
    s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
    printf '"%s"' "$s"
}
