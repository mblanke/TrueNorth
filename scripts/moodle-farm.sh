#!/usr/bin/env bash
# TrueNorth Moodle farm: one Moodle per tenant, wired to TrueNorth.
#
#   scripts/moodle-farm.sh add <node> --port <public port> [--site-url <url>] [--no-build]
#   scripts/moodle-farm.sh status [<node>]
#   scripts/moodle-farm.sh remove <node> [--purge]
#
# add       Builds the node image (infra/platform/moodle), brings the node up as its
#           own compose project (infra/platform/docker/compose.moodle-node.yml),
#           waits for it and registers it as the tenant's Moodle platform in TrueNorth.
#           Safe to re-run: it converges. Courses arrive by publishing an accepted
#           release to the platform (POST /course-releases/{id}/publications).
# status    Node containers and TrueNorth's view of the tenant's Moodle platforms.
# remove    Stops the node and marks its TrueNorth platform inactive. Volumes (and
#           so all Moodle data) are kept unless --purge.
#
# The node is registered in, and serves, the tenant of whoever TN_ADMIN_TOKEN belongs
# to (read from GET /auth/me); set TN_TENANT_ID to check it is the one you meant.
#
# Environment (defaults suit the local dev stack):
#   TN_API             where this script reaches the TrueNorth API   (http://localhost:8081)
#   TN_ADMIN_TOKEN     bearer token of an admin in the target tenant (unset: dev AUTH_DISABLED)
#   TN_TENANT_ID       expected tenant id; the script stops if /auth/me disagrees
#   TN_TOOL_URL        where students' browsers reach the API        ($TN_API)
#   TN_LOGIN_URL       Moodle's login page goes here                 (http://localhost:4200/learning/courses)
#   TN_PUBLIC_KEY_URL  where the node fetches TrueNorth's key        (http://api:8080/lti/public-key.pem)
#   TN_NETWORK         TrueNorth's docker network                    (truenorth_default)
#   TN_API_CONTAINER   the api container, to mark the node a farm node (found on TN_NETWORK)
#   MOODLE_FARM_STATE  per-node secrets and settings, mode 600       (~/.truenorth/moodle-farm)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="$ROOT/infra/platform/docker/compose.moodle-node.yml"
IMAGE="${MOODLE_IMAGE:-truenorth/moodle:5.2.3-tn}"
STATE="${MOODLE_FARM_STATE:-$HOME/.truenorth/moodle-farm}"
TN_API="${TN_API:-http://localhost:8081}"
TN_TOOL_URL="${TN_TOOL_URL:-$TN_API}"
TN_LOGIN_URL="${TN_LOGIN_URL:-http://localhost:4200/learning/courses}"
TN_PUBLIC_KEY_URL="${TN_PUBLIC_KEY_URL:-http://api:8080/lti/public-key.pem}"
TN_NETWORK="${TN_NETWORK:-truenorth_default}"

die() { echo "moodle-farm: $*" >&2; exit 1; }
say() { echo "==> $*"; }

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

check_node() {
  [[ "${1:-}" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || die "node name must be lower-case letters, digits and dashes (got '${1:-}')"
}

tn() {  # tn METHOD PATH [JSON]: call the TrueNorth API, print the body, fail on HTTP >= 400
  local method="$1" path="$2" body="${3:-}" out code
  local -a args=(-sS -X "$method" -H "Content-Type: application/json" -o /dev/stderr -w '%{http_code}')
  [[ -n "${TN_ADMIN_TOKEN:-}" ]] && args+=(-H "Authorization: Bearer $TN_ADMIN_TOKEN")
  [[ -n "$body" ]] && args+=(--data "$body")
  out="$(mktemp)"
  code="$(curl "${args[@]}" "$TN_API$path" 2>"$out")" || { cat "$out" >&2; rm -f "$out"; die "TrueNorth API unreachable at $TN_API"; }
  if (( code >= 400 )); then
    cat "$out" >&2; echo >&2; rm -f "$out"
    die "$method $path returned HTTP $code"
  fi
  cat "$out"; rm -f "$out"
}

caller_tenant() {  # the tenant of the TN_ADMIN_TOKEN holder, checked against TN_TENANT_ID
  local tid
  tid="$(tn GET /auth/me | python3 -c '
import json, sys
print(((json.load(sys.stdin).get("user") or {}).get("tenant_id")) or "")')"
  [[ -n "$tid" ]] || die "GET /auth/me returned no tenant; is TN_ADMIN_TOKEN a registered admin?"
  if [[ -n "${TN_TENANT_ID:-}" && "$TN_TENANT_ID" != "$tid" ]]; then
    die "TN_TENANT_ID=$TN_TENANT_ID but the token belongs to tenant $tid"
  fi
  echo "$tid"
}

compose() {  # compose NODE ARGS...: docker compose for one node, with its saved settings
  local node="$1"; shift
  MOODLE_NODE="$node" MOODLE_IMAGE="$IMAGE" TN_NETWORK="$TN_NETWORK" \
    docker compose --progress quiet -f "$COMPOSE_FILE" --env-file "$STATE/$node.env" "$@"
}

save_settings() {  # save_settings NODE KEY=VALUE...: (re)write the node's non-secret settings
  local node="$1" file="$STATE/$1.env" kv; shift
  local tmp; tmp="$(mktemp)"
  grep -E '^MOODLE_(DB|ADMIN)_PASSWORD=' "$file" > "$tmp"
  for kv in "$@"; do printf '%s\n' "$kv" >> "$tmp"; done
  chmod 600 "$tmp"; mv "$tmp" "$file"
}

secrets_for() {  # create the node's secrets file once; never overwrite it
  local node="$1" file="$STATE/$1.env"
  mkdir -p "$STATE"; chmod 700 "$STATE"
  if [[ ! -f "$file" ]]; then
    umask 077
    {
      echo "MOODLE_DB_PASSWORD=$(openssl rand -hex 24)"
      echo "MOODLE_ADMIN_PASSWORD=Tn-$(openssl rand -hex 12)-Aa1!"
    } > "$file"
    say "generated secrets in $file"
  fi
}

platform_id() {  # platform_id NODE: TrueNorth's id for platform moodle-NODE, or ""
  tn GET /integrations/platforms | NODE="$1" python3 -c '
import json, os, sys
print(next((p["id"] for p in json.load(sys.stdin) if p["slug"] == "moodle-" + os.environ["NODE"]), ""))'
}

cmd_add() {
  local node="${1:-}"; shift || true
  check_node "$node"
  local port="" site_url="" build=1
  while (( $# )); do
    case "$1" in
      --port) port="${2:-}"; shift 2 ;;
      --site-url) site_url="${2:-}"; shift 2 ;;
      --no-build) build=0; shift ;;
      *) die "unknown option $1" ;;
    esac
  done
  [[ "$port" =~ ^[0-9]{2,5}$ ]] || die "--port <public port> is required"
  site_url="${site_url:-http://localhost:$port}"
  site_url="${site_url%/}"

  docker network inspect "$TN_NETWORK" >/dev/null 2>&1 || die "TrueNorth's network $TN_NETWORK does not exist; is the stack up?"
  tn GET /lti/public-key.pem | grep -q "BEGIN PUBLIC KEY" || die "TrueNorth did not serve its public key"
  local tenant; tenant="$(caller_tenant)"

  if (( build )); then
    say "building $IMAGE"
    docker build -q -t "$IMAGE" "$ROOT/infra/platform/moodle" >/dev/null
  fi
  secrets_for "$node"

  save_settings "$node" "MOODLE_SITE_URL=$site_url" "MOODLE_PUBLIC_PORT=$port" \
    "TN_TOOL_URL=$TN_TOOL_URL" "TN_LOGIN_URL=$TN_LOGIN_URL" "TN_PUBLIC_KEY_URL=$TN_PUBLIC_KEY_URL" \
    "TN_TENANT_ID=$tenant"

  say "starting node $node at $site_url for tenant $tenant (a first install takes a few minutes)"
  compose "$node" up -d --wait --wait-timeout 900

  local container reg
  container="$(compose "$node" ps -q moodle)"
  reg="$(docker exec "$container" cat /var/www/moodledata/truenorth-registration.json)" \
    || die "the node came up without wiring itself to TrueNorth; see: docker logs $container"

  say "registering moodle-$node with TrueNorth"
  local body existing
  body="$(REG="$reg" NODE="$node" python3 - <<'PY'
import json, os
reg = json.loads(os.environ["REG"])
node = os.environ["NODE"]
print(json.dumps({
    "name": f"Moodle ({node})", "slug": f"moodle-{node}", "platform_type": "moodle",
    "auth_type": "lti13", "base_url": f"http://moodle-{node}:8080", "is_active": True,
    **{k: reg[k] for k in ("lti_issuer", "lti_client_id", "lti_deployment_id",
                           "lti_auth_login_url", "lti_token_url", "lti_jwks_url")},
}))
PY
)"
  existing="$(platform_id "$node")"
  if [[ -n "$existing" ]]; then
    tn PATCH "/integrations/platforms/$existing" \
      "$(python3 -c 'import json,sys; b=json.loads(sys.argv[1]); [b.pop(k) for k in ("slug","platform_type")]; print(json.dumps(b))' "$body")" >/dev/null
  else
    tn POST /integrations/platforms "$body" >/dev/null
  fi

  # A farm node (app/moodle_farm): its Students' Moodle accounts are bound by their locked
  # TrueNorth id for grades, and TrueNorth may reach its private address. Only the api
  # container can mark one; the API cannot.
  local api_container="${TN_API_CONTAINER:-}"
  [[ -n "$api_container" ]] || api_container="$(docker ps -q --filter "network=$TN_NETWORK" \
    --filter "label=com.docker.compose.service=api" | head -n1)"
  if [[ -n "$api_container" ]]; then
    docker exec "$api_container" python -m app.moodle_backends.install_cli manage "$tenant" "$node" >/dev/null \
      || die "could not mark moodle-$node as a farm node"
    say "marked moodle-$node as a TrueNorth farm node"
  else
    echo "moodle-farm: no api container on $TN_NETWORK; mark the node yourself:" \
      "docker exec <api> python -m app.moodle_backends.install_cli manage $tenant $node" >&2
  fi

  cat <<EOF
==> node $node is up
    Moodle:        $site_url  (students arrive through "Open in Moodle" in TrueNorth)
    admin login:   $site_url/login/index.php?loginredirect=0  (user admin; password in $STATE/$node.env)
    TrueNorth:     platform moodle-$node, server-side address http://moodle-$node:8080
    courses:       publish an accepted release to this platform
                   (POST /course-releases/{release_id}/publications {"platform_id": ...})
EOF
}

cmd_status() {
  local node="${1:-}"
  [[ -n "$node" ]] && check_node "$node"
  docker ps --filter "label=com.docker.compose.project${node:+=tn-moodle-$node}" \
    --format '{{.Label "com.docker.compose.project"}}\t{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep '^tn-moodle-' || echo "no farm nodes running"
  echo
  tn GET /integrations/platforms | python3 -c '
import json, sys
for p in json.load(sys.stdin):
    if p["platform_type"] == "moodle":
        state = "active" if p["is_active"] else "inactive"
        print("%-24s %-9s %s" % (p["slug"], state, p.get("lti_issuer") or "-"))'
}

cmd_remove() {
  local node="${1:-}"; shift || true
  check_node "$node"
  local purge=0
  [[ "${1:-}" == "--purge" ]] && purge=1
  [[ -f "$STATE/$node.env" ]] || die "no node $node (no $STATE/$node.env)"

  local existing; existing="$(platform_id "$node")"
  if [[ -n "$existing" ]]; then
    say "marking moodle-$node inactive in TrueNorth"
    tn PATCH "/integrations/platforms/$existing" '{"is_active": false}' >/dev/null
  fi
  say "stopping node $node"
  if (( purge )); then
    compose "$node" down -v
    rm -f "$STATE/$node.env"
    say "volumes and secrets deleted"
  else
    compose "$node" down
    say "volumes kept; 'add $node --port <port>' brings it back with its data"
  fi
}

case "${1:-}" in
  add) shift; cmd_add "$@" ;;
  status) shift; cmd_status "$@" ;;
  remove) shift; cmd_remove "$@" ;;
  -h|--help|help) usage 0 ;;
  *) usage 1 ;;
esac
