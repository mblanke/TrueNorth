#!/usr/bin/env bash
# Import infra/keycloak/realm-truenorth.json into a throwaway Keycloak and fail if
# the realm does not come up. Uses the image compose.prod.yml pins, so the check
# tracks the version that actually runs.
#
# Until 2026-10-04 the file did not import on Keycloak 24 at all (nested flow
# executions, a removed OPTIONAL requirement, a component-format LDAP block): the
# installer's POST /admin/realms returned 400 and the dev stack had no realm.
# Nothing noticed, because nothing imported it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REALM="$ROOT/infra/keycloak/realm-truenorth.json"
IMAGE="$(grep -Eo 'quay.io/keycloak/keycloak:[^[:space:]"]+' "$ROOT/infra/platform/docker/compose.prod.yml" | head -1)"
NAME="tn-realm-check-$$"
PORT="${KC_CHECK_PORT:-18080}"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "+ importing $(basename "$REALM") into $IMAGE"
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" \
  -e KEYCLOAK_ADMIN=admin -e KEYCLOAK_ADMIN_PASSWORD="$(openssl rand -hex 12)" \
  -v "$REALM:/opt/keycloak/data/import/realm-truenorth.json:ro" \
  "$IMAGE" start-dev --import-realm >/dev/null

for _ in $(seq 1 60); do
  if [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" != "true" ]; then
    break
  fi
  if curl -sf -o /dev/null "http://127.0.0.1:$PORT/realms/truenorth"; then
    echo "realm 'truenorth' imported and serving"
    exit 0
  fi
  sleep 3
done

echo "realm import FAILED; Keycloak log:" >&2
docker logs "$NAME" 2>&1 | grep -E "ERROR|Exception" | head -20 >&2
exit 1
