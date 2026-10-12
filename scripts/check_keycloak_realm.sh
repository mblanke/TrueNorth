#!/usr/bin/env bash
# Import infra/keycloak/realm-truenorth.json into a throwaway Keycloak and fail if
# the realm does not come up. Uses the image compose.prod.yml pins, so the check
# tracks the version that actually runs.
#
# Until 2026-10-04 the file did not import on Keycloak 24 at all (nested flow
# executions, a removed OPTIONAL requirement, a component-format LDAP block): the
# installer's POST /admin/realms returned 400 and the dev stack had no realm.
# Nothing noticed, because nothing imported it.
#
# Since v1.2.0 it also mounts the truenorth login theme (the realm's loginTheme) and
# checks the AC-8 banner: the sign-in page shows it with a required acknowledgement box,
# and a realm localization override (how the installer sets a site's wording,
# roles/tn_keycloak/tasks/banner.yml) replaces the theme's text, quoting included.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REALM="$ROOT/infra/keycloak/realm-truenorth.json"
THEME="$ROOT/infra/keycloak/themes/truenorth"
IMAGE="$(grep -Eo 'quay.io/keycloak/keycloak:[^[:space:]"]+' "$ROOT/infra/platform/docker/compose.prod.yml" | head -1)"
NAME="tn-realm-check-$$"
PORT="${KC_CHECK_PORT:-18080}"
BASE="http://127.0.0.1:$PORT"
ADMIN_PASSWORD="$(openssl rand -hex 12)"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "+ importing $(basename "$REALM") into $IMAGE (theme: truenorth)"
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" \
  -e KEYCLOAK_ADMIN=admin -e KEYCLOAK_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  -v "$REALM:/opt/keycloak/data/import/realm-truenorth.json:ro" \
  -v "$THEME:/opt/keycloak/themes/truenorth:ro" \
  "$IMAGE" start-dev --import-realm >/dev/null

up=0
for _ in $(seq 1 60); do
  if [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" != "true" ]; then
    break
  fi
  if curl -sf -o /dev/null "$BASE/realms/truenorth"; then
    echo "realm 'truenorth' imported and serving"
    up=1
    break
  fi
  sleep 3
done
if [ "$up" != 1 ]; then
  echo "realm import FAILED; Keycloak log:" >&2
  docker logs "$NAME" 2>&1 | grep -E "ERROR|Exception" | head -20 >&2
  exit 1
fi

# ── The AC-8 banner on the sign-in page ────────────────────────────────
# truenorth-web requires PKCE: any well-formed S256 challenge renders the page.
AUTH="$BASE/realms/truenorth/protocol/openid-connect/auth?client_id=truenorth-web&response_type=code"
AUTH="$AUTH&redirect_uri=http%3A%2F%2Flocalhost%3A4200%2F&scope=openid"
AUTH="$AUTH&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256"
page="$(curl -sf "$AUTH")"
fail=0
grep -q 'id="tn-banner"' <<<"$page" || { echo "FAIL: no id=\"tn-banner\" on the sign-in page" >&2; fail=1; }
grep -Eq '<input[^>]*name="tn_ack"[^>]*required' <<<"$page" \
  || { echo "FAIL: no required tn_ack checkbox on the sign-in page" >&2; fail=1; }
grep -q 'Government of Canada training system' <<<"$page" \
  || { echo "FAIL: the theme's default banner text is not shown" >&2; fail=1; }

# ── A site's wording, as the installer writes it (realm localization) ──
token="$(curl -sf --data-urlencode "password=$ADMIN_PASSWORD" -d grant_type=password -d client_id=admin-cli \
  -d username=admin "$BASE/realms/master/protocol/openid-connect/token" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')"
# MessageFormat-escaped as banner.yml does: '' for a quote, '{' and '}' for braces.
curl -sf -o /dev/null -X PUT -H "Authorization: Bearer $token" -H 'Content-Type: text/plain; charset=utf-8' \
  --data-binary "Site notice: it''s '{'monitored'}'." \
  "$BASE/admin/realms/truenorth/localization/en/tnLoginBanner"
page="$(curl -sf "$AUTH")"
grep -q 'Site notice: it&#39;s {monitored}.' <<<"$page" \
  || { echo "FAIL: the realm localization override did not replace the banner text (or was not unescaped)" >&2; fail=1; }

if [ "$fail" != 0 ]; then
  exit 1
fi
echo "login banner: shown, acknowledgement required, site wording applied by realm localization"
