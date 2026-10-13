#!/usr/bin/env bash
# TrueNorth AD test DC: the CI `ad` lane's steps, runnable the same way on a laptop.
#
#   tools/ad-test-dc/run.sh secrets    generate every password (0600, never printed)
#   tools/ad-test-dc/run.sh certs      the test CA, the DC's LDAPS pair, the rogue pair
#   tools/ad-test-dc/run.sh up         build + start dc, rogue, misnamed, keycloak; wait for health
#   tools/ad-test-dc/run.sh federate   realm + LDAP federation via install/roles/tn_keycloak
#   tools/ad-test-dc/run.sh test       tests/integration/test_ad_directory.py (fixture mode)
#   tools/ad-test-dc/run.sh logs       compose logs
#   tools/ad-test-dc/run.sh down       remove the project's containers and volumes
#   tools/ad-test-dc/run.sh clean      down, then delete $AD_TEST_DIR (passwords, CA key)
#   tools/ad-test-dc/run.sh all        secrets certs up federate test
#
# Everything lives under AD_TEST_DIR (default build/ad-test-dc, gitignored):
#   secrets/      one file per account, 0600, in a 0700 directory (mounted read-only)
#   ca-private/   the test CA's and the rogue CA's private keys (0600; never mounted)
#   certs/        ca.crt (public), dc.crt/dc.key, rogue.crt/rogue.key, rogue-ca.crt,
#                 misnamed.crt/misnamed.key (trusted CA, another hostname)
#   truststore/   what Keycloak trusts: ca.crt only
#   state/        the installer's keyed-hash state files (federate)
#
# The project name scopes every container, network and volume: `down` removes exactly
# what `up` made. Never point this at staging; see tools/ad-test-dc/README.md.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
export AD_TEST_DIR="${AD_TEST_DIR:-$ROOT/build/ad-test-dc}"
export AD_PROJECT="${AD_PROJECT:-tn-ad-test}"
export AD_REALM="${AD_REALM:-TNLAB.TEST}"
export AD_NETBIOS="${AD_NETBIOS:-TNLAB}"
export AD_DNS_DOMAIN="${AD_DNS_DOMAIN:-tnlab.test}"
export AD_LDAPS_PORT="${AD_LDAPS_PORT:-16360}"
export AD_KC_PORT="${AD_KC_PORT:-18480}"
# The local recovery administrator federate.yml creates, and the address the
# email-collision AD account is given: the same, on purpose.
export AD_LOCAL_ADMIN_UPN="${AD_LOCAL_ADMIN_UPN:-tnadmin@tn-ad-test.local}"
export AD_COLLISION_EMAIL="${AD_COLLISION_EMAIL:-$AD_LOCAL_ADMIN_UPN}"
PY="${PY:-python3}"

COMPOSE=(docker compose -p "$AD_PROJECT" -f "$HERE/compose.yml")

log() { printf '+ %s\n' "$*"; }

# A password AD's complexity rule accepts: 24 random URL-safe characters, bracketed by
# letters (never a leading "-" an argv parser would take for an option) and one of each
# class. Written straight to a 0600 file; never echoed.
gen_password() {
  local file="$1"
  [ -s "$file" ] && return 0
  (umask 077 && "$PY" -c 'import secrets; print("Tn" + secrets.token_urlsafe(18) + "aA7!", end="")' >"$file")
}

cmd_secrets() {
  install -d -m 0700 "$AD_TEST_DIR" "$AD_TEST_DIR/secrets" "$AD_TEST_DIR/state"
  gen_password "$AD_TEST_DIR/secrets/Administrator"
  gen_password "$AD_TEST_DIR/secrets/keycloak-admin"
  gen_password "$AD_TEST_DIR/secrets/local-admin"
  gen_password "$AD_TEST_DIR/secrets/secrets-key"
  local sam
  while IFS=$'\t' read -r sam _; do
    case "$sam" in ''|'#'*) continue ;; esac
    gen_password "$AD_TEST_DIR/secrets/$sam"
  done <"$HERE/accounts.tsv"
  chmod 0600 "$AD_TEST_DIR"/secrets/*
  log "secrets in $AD_TEST_DIR/secrets ($(ls "$AD_TEST_DIR/secrets" | wc -l | tr -d ' ') files, 0600)"
}

# One CA = key in ca-private/, certificate in certs/. Then a leaf for each name.
make_ca() { # name cn
  local key="$AD_TEST_DIR/ca-private/$1.key" crt="$AD_TEST_DIR/certs/$1.crt"
  [ -s "$crt" ] && return 0
  (umask 077 && openssl genrsa -out "$key" 3072 2>/dev/null)
  openssl req -x509 -new -key "$key" -sha256 -days 825 -subj "/CN=$2" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$crt"
}

make_leaf() { # name ca dnsname
  local key="$AD_TEST_DIR/certs/$1.key" crt="$AD_TEST_DIR/certs/$1.crt"
  [ -s "$crt" ] && return 0
  (umask 077 && openssl genrsa -out "$key" 2048 2>/dev/null)
  openssl req -new -key "$key" -subj "/CN=$3" -out "$AD_TEST_DIR/certs/$1.csr"
  printf 'subjectAltName=DNS:%s,DNS:%s\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature,keyEncipherment\n' \
    "$3" "${3%%.*}" >"$AD_TEST_DIR/certs/$1.ext"
  openssl x509 -req -in "$AD_TEST_DIR/certs/$1.csr" -CA "$AD_TEST_DIR/certs/$2.crt" \
    -CAkey "$AD_TEST_DIR/ca-private/$2.key" -CAcreateserial -CAserial "$AD_TEST_DIR/ca-private/$2.srl" \
    -days 825 -sha256 -extfile "$AD_TEST_DIR/certs/$1.ext" -out "$crt" 2>/dev/null
  rm -f "$AD_TEST_DIR/certs/$1.csr" "$AD_TEST_DIR/certs/$1.ext"
  # Read by root inside the DC container and by nobody else.
  chmod 0600 "$key"
}

cmd_certs() {
  install -d -m 0700 "$AD_TEST_DIR/ca-private"
  install -d -m 0755 "$AD_TEST_DIR/certs" "$AD_TEST_DIR/truststore"
  make_ca ca "TrueNorth AD Test CA (${AD_REALM})"
  make_ca rogue-ca "Untrusted Rogue CA"
  make_leaf dc ca "dc01.${AD_DNS_DOMAIN}"
  make_leaf rogue rogue-ca "rogue.${AD_DNS_DOMAIN}"
  # Trusted CA, wrong name: served as misnamed.<domain>, issued to dc99.<domain>.
  make_leaf misnamed ca "dc99.${AD_DNS_DOMAIN}"
  # The rogue key is read by the container's root; the DC key likewise.
  install -m 0644 "$AD_TEST_DIR/certs/ca.crt" "$AD_TEST_DIR/truststore/tn-ad-test-ca.pem"
  log "test CA (public) $AD_TEST_DIR/certs/ca.crt; CA keys in $AD_TEST_DIR/ca-private (not mounted)"
}

kc_env() {
  AD_KC_ADMIN_PASSWORD="$(cat "$AD_TEST_DIR/secrets/keycloak-admin")"
  [ -n "$AD_KC_ADMIN_PASSWORD" ] || { echo "empty keycloak-admin secret" >&2; exit 2; }
  export AD_KC_ADMIN_PASSWORD
}

cmd_up() {
  cmd_secrets   # both idempotent: only what is missing is made
  cmd_certs
  kc_env
  log "starting project $AD_PROJECT (dc, rogue, keycloak)"
  "${COMPOSE[@]}" up -d --build --wait --wait-timeout 600
}

cmd_federate() {
  kc_env
  log "realm + LDAP federation through install/roles/tn_keycloak"
  ANSIBLE_ROLES_PATH="$ROOT/install/roles" ANSIBLE_STDOUT_CALLBACK=default ANSIBLE_NOCOLOR=1 \
    ansible-playbook -i localhost, -c local "$HERE/federate.yml"
}

cmd_test() {
  local junit="${AD_JUNIT:-$AD_TEST_DIR/junit.xml}"
  AD_MODE=fixture \
  AD_KC_URL="http://127.0.0.1:${AD_KC_PORT}/auth" \
  AD_LDAPS_ADDR="127.0.0.1:${AD_LDAPS_PORT}" \
  AD_DC_HOST="dc01.${AD_DNS_DOMAIN}" \
  AD_CA_FILE="$AD_TEST_DIR/certs/ca.crt" \
  AD_SECRETS_DIR="$AD_TEST_DIR/secrets" \
  AD_ROGUE_URL="ldaps://rogue.${AD_DNS_DOMAIN}:636" \
  AD_MISNAMED_URL="ldaps://misnamed.${AD_DNS_DOMAIN}:636" \
  AD_GROUP_REMOVE_CMD="docker compose -p $AD_PROJECT -f $HERE/compose.yml exec -T dc samba-tool group removemembers {group} {user}" \
  AD_GROUP_ADD_CMD="docker compose -p $AD_PROJECT -f $HERE/compose.yml exec -T dc samba-tool group addmembers {group} {user}" \
  AD_USER_DISABLE_CMD="docker compose -p $AD_PROJECT -f $HERE/compose.yml exec -T dc samba-tool user disable {user}" \
  AD_USER_ENABLE_CMD="docker compose -p $AD_PROJECT -f $HERE/compose.yml exec -T dc samba-tool user enable {user}" \
  AD_GROUP_DELETE_CMD="docker compose -p $AD_PROJECT -f $HERE/compose.yml exec -T dc samba-tool group delete {group}" \
  AD_GROUP_CREATE_CMD="docker compose -p $AD_PROJECT -f $HERE/compose.yml exec -T dc samba-tool group add {group} --groupou=OU=Groups,OU=TrueNorth" \
  AD_FEDERATE_CMD="bash $HERE/run.sh federate" \
  AD_DC_STOP_CMD="docker compose -p $AD_PROJECT -f $HERE/compose.yml stop dc" \
  AD_DC_START_CMD="docker compose -p $AD_PROJECT -f $HERE/compose.yml start dc" \
    "$PY" -m pytest "$ROOT/tests/integration/test_ad_directory.py" -m integration -rs --tb=short \
      --junitxml="$junit" "$@"
}

cmd="${1:-}"
shift || true
case "$cmd" in
  secrets) cmd_secrets ;;
  certs) cmd_certs ;;
  up) cmd_up ;;
  federate) cmd_federate ;;
  test) cmd_test "$@" ;;
  logs) kc_env 2>/dev/null || "${COMPOSE[@]}" logs --no-color --tail=400 ;;
  down) "${COMPOSE[@]}" down -v --remove-orphans ;;
  clean) "${COMPOSE[@]}" down -v --remove-orphans; rm -rf "$AD_TEST_DIR" ;;
  all) cmd_secrets; cmd_certs; cmd_up; cmd_federate; cmd_test "$@" ;;
  *) sed -n '2,22p' "$0"; exit 2 ;;
esac
