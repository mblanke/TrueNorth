#!/usr/bin/env bash
# Integration tests against a real stack: the same sequence locally (`make itest`) and in
# CI (`integration` and `e2e` jobs in .github/workflows/ci.yml).
#
#   scripts/itest.sh up      build + start the stack, migrate, wait until it answers
#   scripts/itest.sh test    run tests/integration against it; fail if nothing passed
#   scripts/itest.sh acceptance  the post-install acceptance suite (scripts/staging_acceptance.py)
#                            against it, after loading the shipped content as the installer does
#   scripts/itest.sh logs    dump compose logs (CI calls this on failure)
#   scripts/itest.sh down    stop it and delete its volumes
#   scripts/itest.sh all     up + test + acceptance, logs on failure, down always (the default)
#
# The stack is compose.dev.yml + compose.itest.yml under its own project name
# (ITEST_PROJECT, default truenorth-itest) and on its own host ports, so a developer's dev
# stack on 8081 is neither touched nor tested by mistake. `down` removes only this
# project's volumes. ITEST_WEB=1 also starts keycloak + web (http://localhost:14200) for
# the Playwright lane. ITEST_LRS=1 also starts the xAPI LRS (lrsql, http://127.0.0.1:18001)
# for the cmi5 lane. ITEST_KEEP=1 leaves the stack up after `all`.
#
# Environment: PY (python with requirements-test.txt; default .venv/bin/python),
# ITEST_OUT (report directory; default build/itest).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROJECT="${ITEST_PROJECT:-truenorth-itest}"
PY="${PY:-$ROOT/.venv/bin/python}"
OUT="${ITEST_OUT:-$ROOT/build/itest}"
API_URL="http://127.0.0.1:18081"
COMPOSE=(docker compose -p "$PROJECT"
  -f infra/platform/docker/compose.dev.yml -f infra/platform/docker/compose.itest.yml)
STORES=(postgres pgbouncer redis opensearch)
# ai-orchestrator runs the mock backend here (compose.itest.yml); test_ai_forge_flow.py needs it.
APP=(api worker-provision worker-scenario worker-telemetry ai-orchestrator)
WEB=(keycloak web)

say() { echo "== itest: $*"; }

# wait_http URL TRIES: poll every 5 s until URL answers 2xx.
wait_http() {
  local url="$1" tries="$2" i
  for ((i = 0; i < tries; i++)); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then say "ready: $url"; return 0; fi
    sleep 5
  done
  echo "itest: $url did not answer within $((tries * 5)) s" >&2
  return 1
}

up() {
  if [[ "$PROJECT" == "truenorth" ]]; then
    echo "itest: refusing project name 'truenorth' (that is the dev stack; down -v would wipe it)" >&2
    exit 2
  fi
  say "stores: ${STORES[*]}"
  "${COMPOSE[@]}" up -d --build --wait --wait-timeout 300 "${STORES[@]}"
  # Alembic owns the schema here, as in production (the API runs with DB_AUTO_CREATE=false).
  # Straight to postgres, not through pgbouncer's transaction pooling.
  say "alembic upgrade head"
  "${COMPOSE[@]}" run --rm --no-deps \
    -e DATABASE_URL=postgresql+psycopg://forge:forge@postgres:5432/forge \
    api alembic upgrade head
  if [[ "${ITEST_LRS:-0}" == "1" ]]; then
    # lrsql has no probe tool in its image; readiness is its own /health, from the host.
    say "lrs"
    "${COMPOSE[@]}" up -d lrs
    wait_http "http://127.0.0.1:18001/health" 60
    # cmi5 AU traffic goes to the LRS with its own credential (docs/cmi5.md): mint one with
    # the dev LRS admin (compose.dev.yml) and hand it to the api through compose.
    say "lrs: minting the cmi5 AU credential"
    CMI5_LRS_AUTH="$(PYTHONPATH="$ROOT/control-plane/api" "$PY" -c \
      'from app.lms.lrsql_admin import mint_au_credential as m; print(m("http://127.0.0.1:18001", "lrsadmin", "lrsadmin"))')"
    export CMI5_LRS_AUTH
  fi
  say "app: ${APP[*]}"
  "${COMPOSE[@]}" up -d --build --wait --wait-timeout 300 "${APP[@]}"
  if [[ "${ITEST_WEB:-0}" == "1" ]]; then
    say "web: ${WEB[*]}"
    "${COMPOSE[@]}" up -d --build --wait --wait-timeout 600 "${WEB[@]}"
    # Neither image has a probe tool, so readiness is checked from the host, through the
    # web container's nginx: the SPA, the API behind /api and the realm behind /auth.
    wait_http "http://localhost:14200/" 60
    wait_http "http://localhost:14200/api/health" 60
    wait_http "http://localhost:14200/auth/realms/truenorth" 120
  fi
  "${COMPOSE[@]}" ps
}

test_cmd() {
  mkdir -p "$OUT"
  say "pytest tests/integration -m integration (API $API_URL)"
  local rc=0
  API_BASE_URL="$API_URL" OPENSEARCH_URL="http://127.0.0.1:19200" INTEGRATION_REQUIRE_API=1 \
    "$PY" -m pytest tests/integration -m integration -rsx --tb=short -q \
    --junitxml="$OUT/junit.xml" || rc=$?
  # pytest exits 0 when every test was skipped; for this lane that is a failure.
  "$PY" scripts/junit_require_pass.py "$OUT/junit.xml" || rc=1
  return "$rc"
}

# The post-install acceptance suite on this stack, set up the way the installer leaves a
# host: OpenSearch bootstrapped (the ingested-at pipeline detection credit is windowed on),
# the shipped content and demo people loaded in-process, then the suite in a one-off api
# container (docs/runbooks/staging-acceptance.md). The one-off runs with AUTH_DISABLED=false
# so each role is its own user, and PROVISIONER_BACKEND=vsphere_api, staging's backend, so
# its ranges are recorded as staging's are (a run on one stays live after its timeline;
# nothing is provisioned, and its one inject touches no host). What this stack does not
# have is declared with --absent; --strict fails any other skip.
ACCEPTANCE_ADMIN="admin@truenorth.local"  # compose.dev's seeded admin
ACCEPTANCE_ABSENT="${ACCEPTANCE_ABSENT:-edge,keycloak,minio,lrs,moodle,backup,vsphere,metrics-token,arc2}"
acceptance() {
  local run=("${COMPOSE[@]}" run --rm --no-deps -T -w /app -e PYTHONPATH=/app -e AUTH_DISABLED=false
    -v "$ROOT:/srcapp:ro" --entrypoint python)
  say "acceptance: OpenSearch bootstrap"
  "$PY" -m telemetry.pipelines.bootstrap --opensearch-url http://127.0.0.1:19200 > "$OUT/acceptance-bootstrap.log" 2>&1
  say "acceptance: shipped content and demo people, in-process (log: $OUT/acceptance-load.log)"
  # Rate limiting off for the loader only: hundreds of imports would otherwise wait out 429s.
  "${run[@]}" -e RATE_LIMIT_ENABLED=false api /srcapp/scripts/load_content_inprocess.py \
    --admin "$ACCEPTANCE_ADMIN" --publish --demo > "$OUT/acceptance-load.log" 2>&1 \
    || { grep -E "^  (FAIL|[0-9]{3} )" "$OUT/acceptance-load.log" | tail -40; return 1; }
  mkdir -p "$OUT/acceptance" && chmod 0777 "$OUT/acceptance"  # the container's user writes the report
  say "acceptance: scripts/staging_acceptance.py --strict (absent: $ACCEPTANCE_ABSENT)"
  "${run[@]}" -e PROVISIONER_BACKEND=vsphere_api -v "$OUT/acceptance:/out" api \
    /srcapp/scripts/staging_acceptance.py --admin "$ACCEPTANCE_ADMIN" --strict \
    --absent "$ACCEPTANCE_ABSENT" --json /out/acceptance.json
}

logs() { "${COMPOSE[@]}" logs --no-color --timestamps --tail=500; }
down() { "${COMPOSE[@]}" --profile tools down -v --remove-orphans; }

all() {
  # Each phase in a subshell with -e live: called as `up || ...`, bash would ignore -e
  # inside the function and carry on past a failed build or migration.
  local rc=0
  set +e
  (set -e; up); rc=$?
  if [[ $rc -eq 0 ]]; then (set -e; test_cmd); rc=$?; fi
  if [[ $rc -eq 0 ]]; then (set -e; acceptance); rc=$?; fi
  set -e
  if [[ $rc -ne 0 ]]; then logs > "$OUT/compose.log" 2>&1 || true; say "compose logs: $OUT/compose.log"; fi
  if [[ "${ITEST_KEEP:-0}" != "1" ]]; then down; fi
  return "$rc"
}

mkdir -p "$OUT"
case "${1:-all}" in
  up) up ;;
  test) test_cmd ;;
  acceptance) acceptance ;;
  logs) logs ;;
  down) down ;;
  all) all ;;
  *) echo "usage: $0 [up|test|acceptance|logs|down|all]" >&2; exit 2 ;;
esac
