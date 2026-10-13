#!/usr/bin/env bash
# Install the chart into a kind cluster and prove it comes up: the migration hook Job
# succeeds, every Deployment rolls out, the api answers /health/ready (database, Redis
# and OpenSearch), and `helm test` passes. Then uninstall.
#
# Needs: kind, kubectl, helm, docker, openssl, and the five images built and tagged
#   tn-local/{api,worker,web,scenario-engine,ai-orchestrator}:ci
# (.github/workflows/helm-kind.yml builds them); it builds tn-local/keycloak:ci itself
# (keycloak.Dockerfile, for deps.yaml). Run from the repo root:
#   KIND_CLUSTER=tn-smoke-$$ bash infra/k8s/kind/smoke.sh
# The cluster is created if missing; set KIND_DELETE=1 to delete it at the end (the
# workflow leaves that to its runner).
set -euo pipefail

CLUSTER="${KIND_CLUSTER:-tn-helm-smoke}"
NS=truenorth
RELEASE=tn
CHART=infra/k8s/helm/truenorth-range
HERE=infra/k8s/kind
TIMEOUT="${SMOKE_TIMEOUT:-12m}"

log() { printf '\n=== %s\n' "$*"; }

dump() {
  log "diagnostics"
  kubectl -n "$NS" get pods,jobs,svc,netpol -o wide || true
  kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -40 || true
  for p in $(kubectl -n "$NS" get pods -o name 2>/dev/null); do
    echo "--- $p"
    # Why a container last stopped (OOMKilled, Error + exit code) and what it said then.
    kubectl -n "$NS" get "$p" -o jsonpath='{range .status.containerStatuses[*]}{.name}: restarts={.restartCount} last={.lastState.terminated.reason} exit={.lastState.terminated.exitCode}{"\n"}{end}' || true
    kubectl -n "$NS" logs "$p" --all-containers --previous --tail=80 2>/dev/null | grep -v IndexWrapper || true
    kubectl -n "$NS" logs "$p" --all-containers --tail=80 | grep -v IndexWrapper || true
  done
}

cleanup() {
  [ -n "${PF_PID:-}" ] && kill "$PF_PID" 2>/dev/null || true
  if [ "${KIND_DELETE:-0}" = 1 ]; then kind delete cluster --name "$CLUSTER" || true; fi
}
trap cleanup EXIT

if ! kind get clusters | grep -qx "$CLUSTER"; then
  log "create kind cluster $CLUSTER"
  kind create cluster --name "$CLUSTER" --wait 120s
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

log "load images"
for svc in api worker web scenario-engine ai-orchestrator; do
  kind load docker-image --name "$CLUSTER" "tn-local/$svc:ci"
done
# deps.yaml's Keycloak, Quarkus build done here rather than in the pod (keycloak.Dockerfile).
# --load: the workflow's current builder is setup-buildx's container builder, which
# otherwise keeps the image to itself.
docker buildx build --load -t tn-local/keycloak:ci -f "$HERE/keycloak.Dockerfile" "$HERE"
kind load docker-image --name "$CLUSTER" tn-local/keycloak:ci

# Generated per run; hex only, so URL-safe for the database and Redis URLs.
gen() { openssl rand -hex 24; }
DB_PW=$(gen); REDIS_PW=$(gen); KC_PW=$(gen)

log "datastores"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create secret generic tn-deps \
  --from-literal=DATABASE_PASSWORD="$DB_PW" --from-literal=REDIS_PASSWORD="$REDIS_PW" \
  --from-literal=KEYCLOAK_ADMIN_PASSWORD="$KC_PW" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create configmap tn-realm --from-file=realm-truenorth.json=infra/keycloak/realm-truenorth.json \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" apply -f "$HERE/deps.yaml"
for d in postgres redis opensearch keycloak; do
  kubectl -n "$NS" rollout status "deploy/$d" --timeout=6m || { dump; exit 1; }
done

log "helm install (the pre-install hook runs alembic upgrade head)"
# --set-string: a hex value such as 1234e567 would otherwise be read as a number.
if ! helm install "$RELEASE" "$CHART" -n "$NS" -f "$HERE/values-kind.yaml" \
  --set-string secrets.databasePassword="$DB_PW" \
  --set-string secrets.redisPassword="$REDIS_PW" \
  --set-string secrets.minioAccessKey="tn-$(openssl rand -hex 4)" \
  --set-string secrets.minioSecretKey="$(gen)" \
  --set-string secrets.csrfSecret="$(gen)" \
  --set-string secrets.secretsKey="$(gen)" \
  --set-string secrets.aiServiceToken="$(gen)" \
  --set-string secrets.metricsScrapeToken="$(gen)" \
  --set-string secrets.keycloakMfaAdminPassword="$KC_PW" \
  --set-string secrets.keycloakAdminClientSecret="$(gen)" \
  --wait --timeout "$TIMEOUT"; then
  dump; exit 1
fi

log "migration hook Job"
kubectl -n "$NS" get job "$RELEASE-truenorth-range-migrate" \
  -o jsonpath='{.status.succeeded}{"\n"}' | grep -qx 1 || { dump; exit 1; }
kubectl -n "$NS" logs "job/$RELEASE-truenorth-range-migrate" --tail=5

log "keycloak-mfa hook Job (the realm configured for MFA before the api started)"
kubectl -n "$NS" get job "$RELEASE-truenorth-range-keycloak-mfa" \
  -o jsonpath='{.status.succeeded}{"\n"}' | grep -qx 1 || { dump; exit 1; }
kubectl -n "$NS" logs "job/$RELEASE-truenorth-range-keycloak-mfa" --tail=20
# The api runs with TN_ENV=production, so TN_MFA_KEYCLOAK_CHECK defaults to refuse.
check_mode="$(kubectl -n "$NS" get configmap "$RELEASE-truenorth-range-config" -o jsonpath='{.data.TN_MFA_KEYCLOAK_CHECK}')"
[ -z "$check_mode" ] || [ "$check_mode" = refuse ] || { echo "the smoke must run the startup check in refuse mode (got $check_mode)" >&2; exit 1; }

log "rollouts"
for d in $(kubectl -n "$NS" get deploy -l app.kubernetes.io/instance="$RELEASE" -o name); do
  kubectl -n "$NS" rollout status "$d" --timeout=3m || { dump; exit 1; }
done

log "api /health/ready through a port-forward"
kubectl -n "$NS" port-forward "svc/$RELEASE-truenorth-range-api" 18080:8080 >/dev/null 2>&1 &
PF_PID=$!
ready=""
for _ in $(seq 30); do
  if ready=$(curl -fsS --max-time 5 http://127.0.0.1:18080/health/ready); then break; fi
  sleep 2
done
echo "$ready"
echo "$ready" | grep -q '"status": *"healthy"' || { dump; exit 1; }
kill "$PF_PID"; PF_PID=""
kubectl -n "$NS" logs "deploy/$RELEASE-truenorth-range-api" --tail=200 \
  | grep -q "MFA: Keycloak realm configured" || { echo "the api did not confirm the realm's MFA configuration" >&2; dump; exit 1; }

log "worker liveness probe (celery ping of <node>@\$HOSTNAME)"
for c in worker-provision worker-scenario worker-telemetry; do
  node=${c#worker-}
  kubectl -n "$NS" exec "deploy/$RELEASE-truenorth-range-$c" -- \
    sh -c "celery -A worker.celery_app inspect ping --timeout 10 -d $node@\$HOSTNAME | grep -q pong" \
    || { dump; exit 1; }
  echo "$c: pong"
done

log "helm test"
helm test "$RELEASE" -n "$NS" --logs --timeout 5m || { dump; exit 1; }

log "helm uninstall"
helm uninstall "$RELEASE" -n "$NS" --wait --timeout 5m
echo "smoke test passed"
