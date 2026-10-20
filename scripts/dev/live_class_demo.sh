#!/usr/bin/env bash
# Dev-only: run TrueNorth locally with a seeded live class (issue #199) for a browser demo.
#
#   bash scripts/dev/live_class_demo.sh start     # seed (idempotent, re-anchors the clock) + serve in the background
#   bash scripts/dev/live_class_demo.sh seed      # re-seed only ("started 40 min ago" again)
#   bash scripts/dev/live_class_demo.sh stop
#   bash scripts/dev/live_class_demo.sh status
#
# Needs: a venv at .venv (requirements-test.txt) and the production SPA build
# (cd control-plane/web && npm ci && npm run build:prod). No Docker, no Keycloak, no
# PostgreSQL: SQLite in .demo/tn.db, a demo sign-in page (scripts/dev/live_class_demo_server.py),
# no Celery broker, no OpenSearch. Everything it writes is under .demo/ (gitignored).
# Accounts: .demo/credentials.txt (mode 600).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEMO="$ROOT/.demo"
PORT="${DEMO_PORT:-18480}"
ORIGIN="http://localhost:$PORT"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
SPA="${DEMO_SPA:-$ROOT/control-plane/web/dist/truenorth-range-web/browser}"
TENANT="5eed1ce0-0000-4000-8000-000000000199"
PIDFILE="$DEMO/server.pid"

mkdir -p "$DEMO"
chmod 700 "$DEMO"

export DEMO_DIR="$DEMO" DEMO_SPA="$SPA" DEMO_ISSUER="$ORIGIN/auth/realms/truenorth"
export PYTHONPATH="$ROOT/control-plane/api:$ROOT/scripts/dev"
export DATABASE_URL="sqlite:///$DEMO/tn.db"
export DB_AUTO_CREATE=false SEED_DEV_DATA=false
export AUTH_DISABLED=false AUTH_BACKEND=generic_oidc
export OIDC_JWKS_URL="http://127.0.0.1:$PORT/auth/realms/truenorth/protocol/openid-connect/certs"
export OIDC_ISSUER="$DEMO_ISSUER" OIDC_AUDIENCE=truenorth-web
export CORS_ORIGINS="$ORIGIN" RATE_LIMIT_ENABLED=false
# No broker and no event store: refused at once, so nothing waits on them.
export REDIS_URL="redis://127.0.0.1:1/15" SEARCH_BACKEND=opensearch OPENSEARCH_URL="http://127.0.0.1:1"
export PLATFORM_TENANT_ID="$TENANT"
# Keep the seeded run as seeded: no clock closing it, no sweep expiring its labs.
export EXERCISE_CLOCK=false SCHEDULER_CLOCK_ENABLED=false LAB_SESSIONS_SWEEP=false WS_HEARTBEAT=false
export COURSE_PUBLISH_RESUME=false MOODLE_RESULTS_PULL_SECONDS=0 RANGE_OP_REDISPATCH_SECONDS=0

running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

seed() { "$PY" "$ROOT/scripts/dev/seed_live_class_demo.py"; }

case "${1:-}" in
  seed) seed ;;
  start)
    [[ -f "$SPA/index.html" ]] || { echo "no SPA build at $SPA (cd control-plane/web && npm ci && npm run build:prod)"; exit 1; }
    if running; then echo "already running (pid $(cat "$PIDFILE")) at $ORIGIN"; seed; exit 0; fi
    seed
    nohup "$PY" -m uvicorn live_class_demo_server:app --host 127.0.0.1 --port "$PORT" \
      > "$DEMO/server.log" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    for _ in $(seq 1 60); do
      curl -fs "$ORIGIN/api/health/live" > /dev/null 2>&1 && { echo "up: $ORIGIN (pid $(cat "$PIDFILE"), log $DEMO/server.log)"; exit 0; }
      running || { echo "server exited; see $DEMO/server.log"; tail -20 "$DEMO/server.log"; exit 1; }
      sleep 1
    done
    echo "not answering after 60 s; see $DEMO/server.log"; exit 1 ;;
  stop)
    if running; then kill "$(cat "$PIDFILE")"; rm -f "$PIDFILE"; echo stopped; else echo "not running"; rm -f "$PIDFILE"; fi ;;
  status)
    if running; then echo "running (pid $(cat "$PIDFILE")) at $ORIGIN"; else echo "not running"; fi ;;
  *) echo "usage: $0 start|seed|stop|status"; exit 2 ;;
esac
