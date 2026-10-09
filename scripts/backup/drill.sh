#!/usr/bin/env bash
# =============================================================================
# TrueNorth Range — backup/restore drill
# =============================================================================
# Proves backup.sh + restore.sh against the REAL production compose file, in a
# disposable compose project that shares nothing with a running stack:
#
#   1. bring up postgres + minio from compose.prod.yml under a unique project name,
#      with an override that swaps the bind-mounted data volumes for throwaway
#      named volumes, renames the fixed networks and drops the production memory
#      sizing (nothing else is changed);
#   2. seed: rows in the app, keycloak and lrs databases (each owned by its own
#      role, as in production) and objects in three buckets (one empty);
#   3. backup.sh, with an escrow key generated for the drill;
#   4. wipe: `down -v` (volumes deleted), bring the services back empty;
#   5. restore.sh;
#   6. verify: per-database, per-table row counts and owners, bucket list and
#      object count are identical before and after; the escrow decrypts back to
#      the exact env file; a second backup of the restored stack succeeds; a
#      backup pointed at a missing env file exits non-zero with an ALERT;
#   8. reinstalled host: `down -v`, FRESH secrets (config/secrets + env), stack up empty;
#      restore-secrets.sh refuses without --force, then writes the escrowed secrets and
#      env back; services recreated with them; restore.sh; state equals the seed, and the
#      restored DB passwords authenticate over TCP while the fresh ones do not;
#   9. tear down (always, via trap).
#
# OpenSearch (DRILL_OPENSEARCH=1, the default): opensearch joins postgres and minio, with
# the snapshot repository bind-mounted from the work dir as compose.prod.yml binds it from
# TN_DATA_ROOT (so it survives `down -v`, as the host directory does), registered on every
# start as the installer's 70-telemetry does. Telemetry indices and aliases are part of the
# compared state, backups snapshot into it, restores bring it back, and a backup beyond
# OPENSEARCH_SNAPSHOT_KEEP prunes the oldest snapshot, after which restore.sh refuses that
# backup before stopping anything. A backup whose snapshot fails exits 5 and keeps the
# data, and restores without --skip-opensearch. Security plugin OFF here (plain http, no auth): the
# drill has no internal CA; the TLS + basic-auth path is the installer's.
#
# Usage: drill.sh             DRILL_PROJECT=<name> to choose the project name,
#                             DRILL_REPORT=<file> to also write the summary there,
#                             DRILL_KEEP=1 to leave the project up for inspection,
#                             DRILL_OPENSEARCH=0 to leave OpenSearch out.
# Needs: docker with compose v2.24+ (for !reset), openssl 3.
# =============================================================================
set -euo pipefail
IFS=$'\n\t'
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
DRILL_PROJECT="${DRILL_PROJECT:-tn-drill-$(date +%s)-$$}"
DRILL_REPORT="${DRILL_REPORT:-}"
DRILL_KEEP="${DRILL_KEEP:-0}"
DRILL_OPENSEARCH="${DRILL_OPENSEARCH:-1}"
OPENSSL="${OPENSSL:-openssl}"

case "${DRILL_PROJECT}" in
    truenorth|docker|tn-r0-pg|"") echo "refusing to drill against project '${DRILL_PROJECT}'" >&2; exit 2 ;;
esac
if [[ -n "$(docker compose -p "${DRILL_PROJECT}" ps -aq 2>/dev/null)" ]]; then
    echo "compose project '${DRILL_PROJECT}' already has containers; pick another DRILL_PROJECT" >&2
    exit 2
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/tn-drill.XXXXXX")"
WORK="$(cd "${WORK}" && pwd -P)"
mkdir -p "${WORK}/backups" "${WORK}/seed" "${WORK}/os-snapshots" "${WORK}/os-certs"
# The opensearch image runs as uid 1000; the installer chowns the real directories to it.
# Here they belong to whoever runs the drill (uid 1001 on a GitHub runner) and umask 077
# makes them private, so open them up. Every bind into config/ must be readable: OpenSearch
# walks the whole config tree at startup (LogConfigurator), and an unreadable certs/ fails it
# with AccessDeniedException; restart: always then crash-loops it. Docker Desktop's bind
# mounts hide this on a Mac. Nothing secret is in these: the drill runs without the plugin.
chmod 0777 "${WORK}/os-snapshots"
chmod 0755 "${WORK}/os-certs"
: >"${WORK}/os-internal_users.yml"
chmod 0644 "${WORK}/os-internal_users.yml"

export COMPOSE_PROJECT_NAME="${DRILL_PROJECT}"
export COMPOSE_FILE="${ROOT}/infra/platform/docker/compose.prod.yml:${WORK}/drill.override.yml"
export ENV_FILE="${WORK}/env.production"
export BACKUP_DIR="${WORK}/backups"
export BACKUP_ESCROW_PUBKEY="${WORK}/escrow.pub"
# The drill's backups are a few MB; the free-space floor is for real hosts.
export BACKUP_MIN_FREE_GB="${BACKUP_MIN_FREE_GB:-1}"
export OPENSSL
DRILL_SERVICES=(postgres minio)
if [[ "${DRILL_OPENSEARCH}" == "1" ]]; then
    DRILL_SERVICES+=(opensearch)
    # What the installer's backup.env says (security off: plain http inside the container).
    export OPENSEARCH_SNAPSHOT_REPO=tn_snapshots
    export OPENSEARCH_SNAPSHOT_KEEP=2
    export OPENSEARCH_URL=http://localhost:9200
fi

# shellcheck source=scripts/backup/lib.sh
source "${HERE}/lib.sh"
TN_LOG_TAG="truenorth-drill"

# On failure, before anything is torn down: each container's state and its last log lines,
# so a CI failure ("opensearch did not come up") says why without a re-run.
dump_containers() {
    local cid
    echo "── drill diagnostics (project ${DRILL_PROJECT}) ──" >&2
    for cid in $(docker ps -aq --filter "label=com.docker.compose.project=${DRILL_PROJECT}" 2>/dev/null); do
        docker inspect --format \
            '{{index .Config.Labels "com.docker.compose.service"}}: status={{.State.Status}} exit={{.State.ExitCode}} oom_killed={{.State.OOMKilled}} restarts={{.RestartCount}} error={{.State.Error}}' \
            "$cid" >&2 2>/dev/null || true
    done
    for cid in $(docker ps -aq --filter "label=com.docker.compose.project=${DRILL_PROJECT}" \
                    --filter "label=com.docker.compose.service=opensearch" 2>/dev/null); do
        echo "── opensearch logs (last 80 lines) ──" >&2
        docker logs --tail 80 "$cid" >&2 2>&1 || true
    done
    if command -v sysctl >/dev/null 2>&1; then
        echo "host: vm.max_map_count=$(sysctl -n vm.max_map_count 2>/dev/null || echo '?')" >&2
    fi
    docker info --format 'docker: {{.ServerVersion}} cpus={{.NCPU}} mem={{.MemTotal}}' >&2 2>/dev/null || true
}

teardown() {
    local rc=$?
    if (( rc != 0 )); then
        dump_containers
    fi
    if [[ "${DRILL_KEEP}" == "1" ]]; then
        log "INFO" "DRILL_KEEP=1: leaving project ${DRILL_PROJECT} and ${WORK}"
    else
        log "INFO" "Tearing down ${DRILL_PROJECT}"
        docker compose -p "${DRILL_PROJECT}" down -v --remove-orphans >/dev/null 2>&1 || true
        rm -rf "${WORK}"
    fi
    if (( rc != 0 )); then
        echo "DRILL FAILED (exit ${rc})" >&2
    fi
}
trap teardown EXIT

rand() { "${OPENSSL}" rand -hex "${1:-16}"; }

# ── Disposable environment ───────────────────────────────────────────────────
# make_env: fresh secrets, persisted one file each under SECRETS_DIR exactly as the
# installer does (config/secrets/<name>), and the env file rendered from them. Run once
# for the original host and again for the "reinstalled" one (step 8).
export SECRETS_DIR="${WORK}/config/secrets"
make_env() {
    rm -rf "${SECRETS_DIR}"
    mkdir -p "${SECRETS_DIR}"
    local n
    for n in postgres_password keycloak_db_password lrs_db_password minio_secret_key redis_password; do
        rand >"${SECRETS_DIR}/${n}"
    done
    printf '%s,%s' "$(rand 32)" "$(rand 32)" >"${SECRETS_DIR}/secrets_key"
    s() { cat "${SECRETS_DIR}/$1"; }
    cat >"${ENV_FILE}" <<EOF
# Generated by scripts/backup/drill.sh for project ${DRILL_PROJECT}. Throwaway.
DOMAIN=drill.invalid
POSTGRES_USER=drill_admin
POSTGRES_PASSWORD=$(s postgres_password)
POSTGRES_DB=drill_app
KEYCLOAK_DB=keycloak
KEYCLOAK_DB_USER=keycloak
KEYCLOAK_DB_PASSWORD=$(s keycloak_db_password)
LRS_DB=lrs
LRS_DB_USER=lrs
LRS_DB_PASSWORD=$(s lrs_db_password)
MINIO_ACCESS_KEY=drillminio
MINIO_SECRET_KEY=$(s minio_secret_key)
REDIS_PASSWORD=$(s redis_password)
TN_SECRETS_KEY=$(s secrets_key)
# Unused by postgres/minio; set only so compose does not warn about them.
FLOWER_USER=x
FLOWER_PASSWORD=x
LRS_ADMIN_USER=x
LRS_ADMIN_PASSWORD=x
LRS_API_KEY=x
LRS_API_SECRET=x
LRS_AUTH=x
GRAFANA_ADMIN_PASSWORD=x
KEYCLOAK_ADMIN_USER=x
KEYCLOAK_ADMIN_PASSWORD=x
LDAP_SERVER=x
LDAP_BASE_DN=x
KEYCLOAK_LDAP_COMPONENT_ID=x
REGISTRATION_ALLOWED_GROUPS=x
REGISTRATION_GROUP_ROLE_MAP={}
REGISTRATION_DEFAULT_PROGRESSION=x
JWT_SECRET=x
CSRF_SECRET=x
# Required by compose.prod.yml (no defaults); never pulled, only postgres/minio start.
PROVISIONER_BACKEND=mock
TN_IMAGE_API=drill.invalid/api:unused
TN_IMAGE_WORKER=drill.invalid/worker:unused
TN_IMAGE_WEB=drill.invalid/web:unused
TN_IMAGE_AI_ORCHESTRATOR=drill.invalid/ai-orchestrator:unused
OPENAI_BASE_URL=http://drill.invalid:4000/v1
OPENAI_API_KEY=
KEYCLOAK_ISSUER=https://drill.invalid/auth/realms/truenorth
# OpenSearch: small, and without the security plugin (no internal CA in the drill).
OPENSEARCH_DISABLE_SECURITY=true
OPENSEARCH_HEAP=512m
OPENSEARCH_USER=
OPENSEARCH_CERTS_DIR=${WORK}/os-certs
OPENSEARCH_INTERNAL_USERS=${WORK}/os-internal_users.yml
EOF
}
make_env

cat >"${WORK}/drill.override.yml" <<EOF
# Drill-only override: isolation, not behaviour. See drill.sh.
services:
  postgres:
    command: ["postgres", "-c", "shared_buffers=128MB", "-c", "max_connections=100"]
    deploy: !reset {}
  minio:
    deploy: !reset {}
  opensearch:
    deploy: !reset {}
volumes:
  pg-data:
    driver_opts: !reset {}
  minio-data:
    driver_opts: !reset {}
  os-data:
    driver_opts: !reset {}
  os-snapshots:
    driver_opts:
      device: ${WORK}/os-snapshots
networks:
  tn-backend:
    name: ${DRILL_PROJECT}-backend
  tn-frontend:
    name: ${DRILL_PROJECT}-frontend
  tn-monitoring:
    name: ${DRILL_PROJECT}-monitoring
EOF

"${OPENSSL}" genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "${WORK}/escrow.key" 2>/dev/null
"${OPENSSL}" pkey -in "${WORK}/escrow.key" -pubout -out "${WORK}/escrow.pub"

compose_init
PG_USER="$(env_get POSTGRES_USER)"
PG_DB="$(env_get POSTGRES_DB)"

start_stack() {
    dc up -d --quiet-pull "${DRILL_SERVICES[@]}" >/dev/null
    # pg_isready answers during initdb's temporary server; wait for the real one.
    local i
    for i in $(seq 1 90); do
        if dc logs postgres 2>/dev/null | grep -q 'PostgreSQL init process complete' \
           && dc exec -T postgres pg_isready -q -U "${PG_USER}" -d "${PG_DB}" 2>/dev/null; then
            break
        fi
        sleep 2
    done
    dc exec -T postgres pg_isready -q -U "${PG_USER}" -d "${PG_DB}" || die "postgres did not come up"
    for i in $(seq 1 60); do
        if mc_run "${WORK}/seed" ls tn >/dev/null 2>&1; then break; fi
        sleep 2
    done
    mc_run "${WORK}/seed" ls tn >/dev/null 2>&1 || die "minio did not come up"
    [[ "${DRILL_OPENSEARCH}" == "1" ]] || return 0
    for i in $(seq 1 90); do
        if os_curl "${OPENSEARCH_URL}/_cluster/health?wait_for_status=yellow&timeout=2s" >/dev/null 2>&1; then break; fi
        sleep 2
    done
    os_curl "${OPENSEARCH_URL}/_cluster/health?wait_for_status=yellow&timeout=5s" >/dev/null || die "opensearch did not come up"
    # What 70-telemetry does (telemetry.pipelines.bootstrap.register_snapshot_repository).
    os_put "_snapshot/${OPENSEARCH_SNAPSHOT_REPO}" \
        '{"type":"fs","settings":{"location":"/usr/share/opensearch/snapshots","compress":true}}'
}

os_put() { # os_put <path> <json body>
    os_curl -X PUT -H 'Content-Type: application/json' "${OPENSEARCH_URL}/$1" -d "$2" >/dev/null
}

psql_as() { # psql_as <role> <db> <sql>
    dc exec -T postgres psql -v ON_ERROR_STOP=1 -q -U "$1" -d "$2" -c "$3"
}

# Every user table in every database: "<db> <schema.table> <owner> <rows>"; then
# the bucket list and the object count.
snapshot() {
    local db
    while IFS= read -r db; do
        [[ -n "$db" ]] || continue
        # </dev/null: `exec -T` would otherwise swallow the rest of the db list.
        dc exec -T postgres psql -U "${PG_USER}" -d "$db" -v ON_ERROR_STOP=1 -At -F ' ' -c "
            select current_database(), schemaname||'.'||tablename, tableowner,
                   (xpath('/row/c/text()', query_to_xml(
                       format('select count(*) as c from %I.%I', schemaname, tablename),
                       false, true, '')))[1]::text
            from pg_tables
            where schemaname not in ('pg_catalog', 'information_schema')
            order by 2" </dev/null
    done < <(dc exec -T postgres psql -U "${PG_USER}" -d "${PG_DB}" -Atc \
        "select datname from pg_database where not datistemplate order by 1" | tr -d '\r')
    printf 'minio buckets %s\n' "$(mc_run "${WORK}/seed" ls tn | awk '{print $NF}' | tr -d '/' | sort | paste -sd, -)"
    printf 'minio objects %s\n' "$(mc_run "${WORK}/seed" ls --recursive tn | grep -c . || true)"
    if [[ "${DRILL_OPENSEARCH}" == "1" ]]; then
        os_curl "${OPENSEARCH_URL}/_refresh" >/dev/null
        os_curl "${OPENSEARCH_URL}/_cat/indices?h=index,docs.count&s=index" | tr -s ' ' | grep -v '^\.' \
            | sed 's/^/opensearch index /' || true
        os_curl "${OPENSEARCH_URL}/_cat/aliases?h=alias,index&s=alias" | tr -s ' ' | grep -v '^\.' \
            | sed 's/^/opensearch alias /' || true
    fi
}

# ── 1. Up ────────────────────────────────────────────────────────────────────
log "INFO" "Drill project ${DRILL_PROJECT} (work dir ${WORK})"
start_stack

# ── 2. Seed ──────────────────────────────────────────────────────────────────
log "INFO" "Seeding..."
psql_as "${PG_USER}" "${PG_DB}" "
    create table ranges (id serial primary key, name text not null, created timestamptz default now());
    insert into ranges (name) select 'range-' || g from generate_series(1, 1000) g;
    create table enrollments (id bigserial primary key, range_id int references ranges(id), student text);
    insert into enrollments (range_id, student) select 1 + (g % 1000), 'student-' || g from generate_series(1, 2500) g;"
# Companion databases, written by their own roles exactly as Keycloak and the LRS do.
dc exec -T postgres psql -v ON_ERROR_STOP=1 -q -U "${PG_USER}" -d keycloak -c "
    set role keycloak;
    create table user_entity (id text primary key, username text);
    insert into user_entity select 'u' || g, 'user' || g from generate_series(1, 137) g;"
dc exec -T postgres psql -v ON_ERROR_STOP=1 -q -U "${PG_USER}" -d lrs -c "
    set role lrs;
    create table xapi_statement (id uuid primary key default gen_random_uuid(), payload jsonb);
    insert into xapi_statement (payload) select jsonb_build_object('n', g) from generate_series(1, 420) g;"

mkdir -p "${WORK}/seed/truenorth-artifacts/aar" "${WORK}/seed/truenorth-uploads"
for i in $(seq 1 23); do rand 64 >"${WORK}/seed/truenorth-artifacts/aar/report-${i}.json"; done
for i in $(seq 1 9); do rand 256 >"${WORK}/seed/truenorth-uploads/file-${i}.bin"; done
for b in truenorth-artifacts truenorth-uploads truenorth-empty; do
    mc_run "${WORK}/seed" mb --ignore-existing "tn/${b}" >/dev/null
done
mc_run "${WORK}/seed" mirror --quiet /backup/truenorth-artifacts tn/truenorth-artifacts >/dev/null
mc_run "${WORK}/seed" mirror --quiet /backup/truenorth-uploads tn/truenorth-uploads >/dev/null

# Telemetry: a per-range index and a rollover family behind its write alias, as in production.
if [[ "${DRILL_OPENSEARCH}" == "1" ]]; then
    os_put "exercise-events-000001" '{"aliases":{"exercise-events":{"is_write_index":true}}}'
    os_bulk() { # os_bulk <index> <count>
        local body="" i
        for i in $(seq 1 "$2"); do
            body+="{\"index\":{\"_index\":\"$1\"}}"$'\n'"{\"@timestamp\":\"2026-10-09T00:00:00Z\",\"n\":${i}}"$'\n'
        done
        os_curl -X POST -H 'Content-Type: application/x-ndjson' "${OPENSEARCH_URL}/_bulk?refresh=true" \
            --data-binary "${body}" | grep -q '"errors":false' || die "seeding $1 failed"
    }
    os_bulk range-drill-0001 37
    os_bulk exercise-events 12
fi

snapshot >"${WORK}/before.txt"

# ── 3. Backup ────────────────────────────────────────────────────────────────
log "INFO" "Backup..."
"${HERE}/backup.sh"
BACKUP="$(find "${BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -name 'truenorth-backup-*' | sort | tail -n 1)"
[[ -n "${BACKUP}" ]] || die "backup.sh produced no backup directory"

# ── 4. Wipe ──────────────────────────────────────────────────────────────────
log "INFO" "Wipe (down -v) and bring up empty..."
dc down -v >/dev/null 2>&1
start_stack
# Telemetry written after the backup: a restore replaces the indices, so this must go.
if [[ "${DRILL_OPENSEARCH}" == "1" ]]; then os_bulk range-drill-late 5; fi
snapshot >"${WORK}/wiped.txt"
if cmp -s "${WORK}/before.txt" "${WORK}/wiped.txt"; then
    die "wipe did not change anything; the drill would prove nothing"
fi

# ── 5. Restore ───────────────────────────────────────────────────────────────
log "INFO" "Restore..."
"${HERE}/restore.sh" "${BACKUP}" --force --no-restart

# ── 6. Verify ────────────────────────────────────────────────────────────────
snapshot >"${WORK}/after.txt"
if ! diff -u "${WORK}/before.txt" "${WORK}/after.txt" >&2; then
    die "restored state differs from the seeded state"
fi
"${HERE}/escrow-open.sh" "${BACKUP}" "${WORK}/escrow.key" "${WORK}/env.recovered" 2>/dev/null
cmp -s "${ENV_FILE}" "${WORK}/env.recovered" || die "escrow did not decrypt to the original env file"
grep -q '^TN_SECRETS_KEY=' "${WORK}/env.recovered" || die "escrowed env has no TN_SECRETS_KEY"
if grep -rqs -- "$(env_get TN_SECRETS_KEY | cut -d, -f1)" "${BACKUP}"; then
    die "TN_SECRETS_KEY found in plain text inside the backup"
fi

sleep 1  # distinct timestamp for the second backup
"${HERE}/backup.sh" >/dev/null
[[ "$(find "${BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -name 'truenorth-backup-*' | grep -c .)" == 2 ]] \
    || die "second backup (of the restored stack) did not produce a backup"

set +e
ENV_FILE="${WORK}/missing.env" "${HERE}/backup.sh" >/dev/null 2>"${WORK}/fail.err"
fail_rc=$?
set -e
(( fail_rc != 0 )) || die "backup.sh with a missing env file exited 0"
grep -q 'ALERT\|ERROR' "${WORK}/fail.err" || die "backup.sh failure produced no alert on stderr"

# ── 8. A REINSTALLED host: fresh secrets, then the backup's restored over them ──
# The disaster case: the host was rebuilt and the installer ran again, so the stack came
# up with FRESH generated secrets and empty data. restore-secrets.sh puts the escrowed
# secrets back (refusing until --force, since they differ), the services restart with
# them, restore.sh puts the data back, and the restored passwords must then actually
# authenticate over TCP (and the fresh ones must not).
log "INFO" "Reinstalled host: fresh secrets, empty data..."
cp -R "${SECRETS_DIR}" "${WORK}/secrets.orig"
cp "${ENV_FILE}" "${WORK}/env.orig"
dc down -v >/dev/null 2>&1
make_env
cp -R "${SECRETS_DIR}" "${WORK}/secrets.fresh"
if diff -rq "${WORK}/secrets.orig" "${SECRETS_DIR}" >/dev/null 2>&1; then
    die "the reinstall did not generate fresh secrets; step 8 would prove nothing"
fi
start_stack

set +e
"${HERE}/restore-secrets.sh" "${BACKUP}" "${WORK}/escrow.key" --secrets-dir "${SECRETS_DIR}" \
    --env-file "${ENV_FILE}" >/dev/null 2>"${WORK}/rs.err"
rs_rc=$?
set -e
if (( rs_rc == 0 )) || ! grep -q -- '--force' "${WORK}/rs.err"; then
    die "restore-secrets.sh overwrote differing secrets without --force (rc ${rs_rc})"
fi
"${HERE}/restore-secrets.sh" "${BACKUP}" "${WORK}/escrow.key" --secrets-dir "${SECRETS_DIR}" \
    --env-file "${ENV_FILE}" --force 2>/dev/null
diff -r "${WORK}/secrets.orig" "${SECRETS_DIR}" >&2 || die "restored secrets differ from the originals"
cmp -s "${WORK}/env.orig" "${ENV_FILE}" || die "restored env file differs from the original"
[[ -n "$(find "$(dirname "${SECRETS_DIR}")" -maxdepth 1 -name 'secrets.replaced-*')" ]] \
    || die "restore-secrets.sh --force did not keep the replaced secrets"

# What the re-run installer does next: recreate the services with the restored env.
dc up -d --force-recreate postgres minio >/dev/null
for _ in $(seq 1 60); do
    dc exec -T postgres pg_isready -q -U "${PG_USER}" -d "${PG_DB}" 2>/dev/null \
        && mc_run "${WORK}/seed" ls tn >/dev/null 2>&1 && break
    sleep 2
done
"${HERE}/restore.sh" "${BACKUP}" --force --no-restart
snapshot >"${WORK}/after-reinstall.txt"
diff -u "${WORK}/before.txt" "${WORK}/after-reinstall.txt" >&2 \
    || die "state restored onto the reinstalled host differs from the seeded state"

# Password authentication over the network, as pgbouncer, Keycloak and the LRS connect.
pg_cid="$(service_cid postgres)"
pg_net="$(container_network "${pg_cid}")"
pg_image="$(docker inspect --format '{{.Config.Image}}' "${pg_cid}")"
pg_login() { # pg_login <role> <db> <password> — exit status of a TCP login
    PGPASSWORD="$3" docker run --rm --network "${pg_net}" -e PGPASSWORD "${pg_image}" \
        psql -h postgres -U "$1" -d "$2" -Atc 'select 1' >/dev/null 2>&1
}
for spec in "${PG_USER}:${PG_DB}:postgres_password" "keycloak:keycloak:keycloak_db_password" "lrs:lrs:lrs_db_password"; do
    IFS=: read -r role db name <<<"${spec}"
    pg_login "${role}" "${db}" "$(cat "${SECRETS_DIR}/${name}")" \
        || die "restored ${name} does not authenticate as ${role}"
    if pg_login "${role}" "${db}" "$(cat "${WORK}/secrets.fresh/${name}")"; then
        die "the reinstall's fresh ${name} still authenticates as ${role}: the restore did not take"
    fi
done
log "INFO" "Reinstalled host: secrets restored, data restored, restored passwords authenticate"

# ── OpenSearch snapshot retention ────────────────────────────────────────────
# Two snapshots so far (the first backup's and the second's). A third backup with KEEP=2
# deletes the first; restore.sh must then refuse the first backup before stopping anything.
OS_SUMMARY="not drilled (DRILL_OPENSEARCH=0)"
if [[ "${DRILL_OPENSEARCH}" == "1" ]]; then
    first_snap="$(sed -nE 's/.*"snapshot":"(tn-[^"]+)".*/\1/p' "${BACKUP}/manifest.json")"
    [[ -n "${first_snap}" ]] || die "the first backup's manifest names no OpenSearch snapshot"
    sleep 1
    third_rc=0
    "${HERE}/backup.sh" >/dev/null || third_rc=$?
    (( third_rc == 0 )) || die "third backup exited ${third_rc} (5: its snapshot failed; is opensearch still up? $(dc ps --services --status running | paste -sd, -))"
    snaps="$(os_curl "${OPENSEARCH_URL}/_cat/snapshots/${OPENSEARCH_SNAPSHOT_REPO}?h=id" | tr -d ' \r')"
    [[ "$(printf '%s\n' "${snaps}" | grep -c .)" == 2 ]] || die "expected 2 snapshots after pruning, got: ${snaps}"
    if printf '%s\n' "${snaps}" | grep -qx "${first_snap}"; then
        die "OPENSEARCH_SNAPSHOT_KEEP=2 did not prune the oldest snapshot ${first_snap}"
    fi
    running_before="$(dc ps --services --status running | sort | paste -sd, -)"
    set +e
    "${HERE}/restore.sh" "${BACKUP}" --force --no-restart >/dev/null 2>"${WORK}/os-pruned.err"
    pruned_rc=$?
    set -e
    if (( pruned_rc == 0 )) || ! grep -q -- '--skip-opensearch' "${WORK}/os-pruned.err"; then
        die "restore.sh of a backup whose snapshot was pruned did not refuse (rc ${pruned_rc})"
    fi
    [[ "$(dc ps --services --status running | sort | paste -sd, -)" == "${running_before}" ]] \
        || die "restore.sh stopped services before refusing a pruned snapshot"
    # A snapshot that fails (here: a repository nobody registered) must not cost the data:
    # exit 5, the backup kept with the failure in its manifest, and it restores without
    # --skip-opensearch, leaving telemetry alone.
    sleep 1
    set +e
    OPENSEARCH_SNAPSHOT_REPO=tn_unregistered "${HERE}/backup.sh" >/dev/null 2>"${WORK}/os-fail.err"
    os_fail_rc=$?
    set -e
    (( os_fail_rc == 5 )) || die "backup.sh with a failing snapshot exited ${os_fail_rc}, not 5"
    grep -q 'ALERT.*OpenSearch telemetry snapshot' "${WORK}/os-fail.err" || die "a failed snapshot raised no alert"
    FAILED_SNAP_BACKUP="$(find "${BACKUP_DIR}" -mindepth 1 -maxdepth 1 -type d -name 'truenorth-backup-*' | sort | tail -n 1)"
    grep -q '"opensearch": {"status":"failed"' "${FAILED_SNAP_BACKUP}/manifest.json" \
        || die "the manifest of ${FAILED_SNAP_BACKUP} does not record the failed snapshot"
    "${HERE}/restore.sh" "${FAILED_SNAP_BACKUP}" --force --no-restart >"${WORK}/os-fail-restore.log" 2>&1
    grep -q 'WARN.*snapshot FAILED' "${WORK}/os-fail-restore.log" || die "restore.sh gave no warning about the failed snapshot"
    # Not restoring telemetry, restore.sh stopped opensearch with the rest (--no-restart
    # leaves it down); bring it back before comparing state.
    start_stack
    snapshot >"${WORK}/after-failed-snapshot.txt"
    diff -u "${WORK}/before.txt" "${WORK}/after-failed-snapshot.txt" >&2 \
        || die "restoring the backup whose snapshot failed did not give back the seeded state"
    OS_SUMMARY="snapshot ${first_snap} restored twice; pruned at KEEP=2, after which restore.sh refuses that backup up front; a failed snapshot exits 5 and its backup restores the rest"
    log "INFO" "OpenSearch: ${OS_SUMMARY}"
fi

# ── Report ───────────────────────────────────────────────────────────────────
{
    echo "TrueNorth backup drill — PASS"
    echo "project: ${DRILL_PROJECT}   backup: $(basename "${BACKUP}")"
    echo "databases: $(sed -n 's/.*"databases": \[\(.*\)\]}.*/\1/p' "${BACKUP}/manifest.json" | grep -o '"name":"[^"]*"' | cut -d'"' -f4 | paste -sd, -)"
    echo "state before backup == state after restore:"
    sed 's/^/  /' "${WORK}/after.txt"
    echo "escrow: decrypts to the original env file; TN_SECRETS_KEY not in plain text"
    echo "second backup after restore: ok; failure path: exit ${fail_rc} with alert"
    echo "reinstalled host: escrowed secrets restored over fresh ones (refused without --force),"
    echo "  data restored, restored DB passwords authenticate over TCP and the fresh ones do not"
    echo "opensearch: ${OS_SUMMARY}"
} | tee "${WORK}/report.txt"
if [[ -n "${DRILL_REPORT}" ]]; then cp "${WORK}/report.txt" "${DRILL_REPORT}"; fi
