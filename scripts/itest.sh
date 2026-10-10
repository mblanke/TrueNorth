#!/usr/bin/env bash
# Integration tests against a real stack: the same sequence locally (`make itest`) and in
# CI (`integration` and `e2e` jobs in .github/workflows/ci.yml).
#
#   scripts/itest.sh up      build + start the stack, migrate, wait until it answers
#   scripts/itest.sh test    run tests/integration against it; fail if nothing passed
#   scripts/itest.sh logs    dump compose logs (CI calls this on failure)
#   scripts/itest.sh down    stop it and delete its volumes
#   scripts/itest.sh all     up + test, logs on failure, down always (the default)
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

logs() { "${COMPOSE[@]}" logs --no-color --timestamps --tail=500; }
down() { "${COMPOSE[@]}" --profile tools down -v --remove-orphans; }

all() {
  # Each phase in a subshell with -e live: called as `up || ...`, bash would ignore -e
  # inside the function and carry on past a failed build or migration.
  local rc=0
  set +e
  (set -e; up); rc=$?
  if [[ $rc -eq 0 ]]; then (set -e; test_cmd); rc=$?; fi
  set -e
  if [[ $rc -ne 0 ]]; then logs > "$OUT/compose.log" 2>&1 || true; say "compose logs: $OUT/compose.log"; fi
  if [[ "${ITEST_KEEP:-0}" != "1" ]]; then down; fi
  return "$rc"
}

mkdir -p "$OUT"
case "${1:-all}" in
  up) up ;;
  test) test_cmd ;;
  logs) logs ;;
  down) down ;;
  all) all ;;
  *) echo "usage: $0 [up|test|logs|down|all]" >&2; exit 2 ;;
esac
