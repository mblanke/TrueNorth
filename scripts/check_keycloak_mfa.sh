#!/usr/bin/env bash
# Prove the MFA configuration on the Keycloak image compose.prod.yml pins (CI job
# keycloak-mfa). A throwaway Keycloak imports infra/keycloak/realm-truenorth.json (with
# the truenorth login theme mounted when it exists), scripts/keycloak_mfa.py applies
# install/roles/tn_keycloak/files/mfa-spec.json (twice: the second run must change
# nothing), and scripts/check_keycloak_mfa.py drives real browser sign-ins:
# enrolment forced for privileged groups, amr/acr in the tokens, Students untouched,
# step-up, the test-host demo exemption, and the break-glass reset. Needs Docker, curl,
# python3.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REALM="$ROOT/infra/keycloak/realm-truenorth.json"
THEME="$ROOT/infra/keycloak/themes/truenorth"
SPEC="$ROOT/install/roles/tn_keycloak/files/mfa-spec.json"
IMAGE="$(grep -Eo 'quay.io/keycloak/keycloak:[^[:space:]"]+' "$ROOT/infra/platform/docker/compose.prod.yml" | head -1)"
NAME="tn-mfa-check-$$"
PORT="${KC_CHECK_PORT:-18081}"
BASE="http://127.0.0.1:$PORT"
ADMIN_PASSWORD="$(openssl rand -hex 12)"
# The API's Admin API client, made by keycloak_mfa.py --ensure-api-client (the Helm hook's
# path) and used by check (viii) as the API's startup check uses it.
TN_API_ADMIN_CLIENT_ID=truenorth-api-admin
TN_API_ADMIN_CLIENT_SECRET="$(openssl rand -hex 24)"
export TN_API_ADMIN_CLIENT_ID TN_API_ADMIN_CLIENT_SECRET

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

MOUNTS=(-v "$REALM:/opt/keycloak/data/import/realm-truenorth.json:ro")
BANNER=()
if [ -d "$THEME" ]; then
  MOUNTS+=(-v "$THEME:/opt/keycloak/themes/truenorth:ro")
  BANNER=(--expect-banner)
fi

echo "+ $IMAGE with $(basename "$REALM")${BANNER:+ and the truenorth theme}"
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" \
  -e KEYCLOAK_ADMIN=admin -e KEYCLOAK_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  "${MOUNTS[@]}" "$IMAGE" start-dev --import-realm >/dev/null

for _ in $(seq 1 60); do
  if curl -sf -o /dev/null "$BASE/realms/truenorth"; then break; fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" != "true" ]; then break; fi
  sleep 3
done
curl -sf -o /dev/null "$BASE/realms/truenorth" || {
  echo "Keycloak did not come up:" >&2
  docker logs "$NAME" 2>&1 | grep -E "ERROR|Exception" | head -20 >&2
  exit 1
}

# A fresh admin token for each step (admin-cli tokens live about 60 s). Passed on stdin.
admin_token() {
  curl -sf --data-urlencode "password=$ADMIN_PASSWORD" -d grant_type=password -d client_id=admin-cli \
    -d username=admin "$BASE/realms/master/protocol/openid-connect/token" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'
}

echo "+ groups and the identity scope (as roles/tn_keycloak creates them)"
printf '%s\n' "$ADMIN_PASSWORD" | python3 "$ROOT/scripts/check_keycloak_mfa.py" --base "$BASE" --create-groups

echo "+ keycloak_mfa.py (first run)"
admin_token | python3 "$ROOT/scripts/keycloak_mfa.py" --base "$BASE" --realm truenorth --spec "$SPEC" \
    --ensure-api-client "$TN_API_ADMIN_CLIENT_ID"
echo "+ keycloak_mfa.py (second run: must report ok)"
second="$(admin_token | python3 "$ROOT/scripts/keycloak_mfa.py" --base "$BASE" --realm truenorth --spec "$SPEC" \
    --ensure-api-client "$TN_API_ADMIN_CLIENT_ID")"
echo "$second"
if [ "$(echo "$second" | tail -1)" != "ok" ]; then
  echo "keycloak_mfa.py is not idempotent: the second run changed something" >&2
  exit 1
fi

echo "+ browser sign-ins"
printf '%s\n' "$ADMIN_PASSWORD" | python3 "$ROOT/scripts/check_keycloak_mfa.py" --base "$BASE" ${BANNER[@]+"${BANNER[@]}"}
