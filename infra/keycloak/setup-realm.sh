#!/usr/bin/env bash
# ------------------------------------------------------------------
# setup-realm.sh — Import the TrueNorth realm into a running Keycloak
# Usage: ./setup-realm.sh [KEYCLOAK_URL] [ADMIN_USER] [ADMIN_PASS]
# ------------------------------------------------------------------
set -euo pipefail

KEYCLOAK_URL="${1:-http://localhost:8080}"
ADMIN_USER="${2:-admin}"
ADMIN_PASS="${3:-admin}"
REALM_JSON="$(cd "$(dirname "$0")" && pwd)/realm-truenorth.json"
KCADM="${KEYCLOAK_HOME:-/opt/keycloak}/bin/kcadm.sh"

echo "========================================"
echo " TrueNorth Range — Keycloak Realm Setup"
echo "========================================"
echo "Keycloak URL : $KEYCLOAK_URL"
echo "Admin user   : $ADMIN_USER"
echo "Realm file   : $REALM_JSON"
echo ""

# ---- Wait for Keycloak to be healthy ----
echo "[1/4] Waiting for Keycloak to become healthy..."
MAX_RETRIES=60
RETRY_INTERVAL=5
for i in $(seq 1 $MAX_RETRIES); do
    if curl -sf "${KEYCLOAK_URL}/health/ready" > /dev/null 2>&1 || \
       curl -sf "${KEYCLOAK_URL}/realms/master" > /dev/null 2>&1; then
        echo "  Keycloak is healthy (attempt $i)."
        break
    fi
    if [ "$i" -eq "$MAX_RETRIES" ]; then
        echo "  ERROR: Keycloak did not become healthy after $((MAX_RETRIES * RETRY_INTERVAL))s." >&2
        exit 1
    fi
    echo "  Attempt $i/$MAX_RETRIES — retrying in ${RETRY_INTERVAL}s..."
    sleep "$RETRY_INTERVAL"
done

# ---- Authenticate to Keycloak admin CLI ----
echo "[2/4] Authenticating as '$ADMIN_USER'..."
$KCADM config credentials \
    --server "$KEYCLOAK_URL" \
    --realm master \
    --user "$ADMIN_USER" \
    --password "$ADMIN_PASS"
echo "  Authenticated."

# ---- Import realm ----
echo "[3/4] Importing realm from $REALM_JSON..."
EXISTING=$($KCADM get realms --fields realm --format csv 2>/dev/null | grep -c "truenorth" || true)
if [ "$EXISTING" -gt 0 ]; then
    echo "  Realm 'truenorth' already exists — updating..."
    $KCADM update realms/truenorth -f "$REALM_JSON"
else
    echo "  Creating realm 'truenorth'..."
    $KCADM create realms -f "$REALM_JSON"
fi
echo "  Realm imported."

# ---- Create initial admin (idempotent) ----
echo "[4/4] Ensuring platform admin user exists..."
ADMIN_EXISTS=$($KCADM get users -r truenorth -q "email=admin@truenorth.local" --format csv 2>/dev/null | grep -c "admin@truenorth.local" || true)
if [ "$ADMIN_EXISTS" -eq 0 ]; then
    echo "  Admin user created during realm import."
else
    echo "  Admin user already exists."
fi

echo ""
echo "========================================"
echo " Setup Complete"
echo "========================================"
echo ""
echo "  Keycloak Console : ${KEYCLOAK_URL}/admin/master/console/"
echo "  TrueNorth Realm  : ${KEYCLOAK_URL}/admin/master/console/#/truenorth"
echo ""
echo "  Dev users:"
echo "    admin@truenorth.local      / admin      (admin role)"
echo "    instructor@truenorth.local / instructor (instructor role)"
echo "    trainee@truenorth.local    / trainee    (trainee role)"
echo ""
echo "  SPA client  : truenorth-web  (public, PKCE)"
echo "  API client  : truenorth-api  (confidential, secret=CHANGE_ME_IN_PRODUCTION)"
echo "  CLI client  : truenorth-cli  (public, device flow)"
echo ""
echo "  Token endpoint:"
echo "    ${KEYCLOAK_URL}/realms/truenorth/protocol/openid-connect/token"
echo ""