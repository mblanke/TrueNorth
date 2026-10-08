#!/bin/sh
# Writes the SPA's runtime config from the container environment, so one web image
# serves every deployment. Run by the nginx image's /docker-entrypoint.sh before nginx
# starts (any *.sh in /docker-entrypoint.d/), as the unprivileged `nginx` user.
#
#   TN_KEYCLOAK_URL          browser-facing Keycloak base URL    (build default: /auth)
#   TN_KEYCLOAK_REALM        realm                               (build default: truenorth)
#   TN_KEYCLOAK_CLIENT_ID    public OIDC client                  (build default: truenorth-web)
#   TN_ADMIN_KEYCLOAK_URL    Admin > Services: Keycloak console  (default: <keycloak url>/admin/)
#   TN_ADMIN_MINIO_URL       Admin > Services: MinIO console     (unset: card hidden)
#   TN_ADMIN_DASHBOARDS_URL  Admin > Services: OpenSearch Dashboards (unset: card hidden)
#   TN_ADMIN_AI_URL          Admin > Services: AI orchestrator API docs (unset: card hidden)
#
# The web root is root-owned and read-only to this user, so the file goes to
# /tmp/truenorth-runtime/config.json; nginx.conf serves /assets/config.json from there
# and falls back to the image's built-in copy ({}) when it is absent. None set: no file,
# and the build defaults in environment.prod.ts apply.
#
# There is deliberately no variable for disabling auth: the SPA reads only the Keycloak
# endpoint and the admin links from this file.
set -eu

dir="${TN_RUNTIME_CONFIG_DIR:-/tmp/truenorth-runtime}"
target="$dir/config.json"
mkdir -p "$dir"

url="${TN_KEYCLOAK_URL:-}"
realm="${TN_KEYCLOAK_REALM:-}"
client="${TN_KEYCLOAK_CLIENT_ID:-}"
admin_kc="${TN_ADMIN_KEYCLOAK_URL:-}"
admin_minio="${TN_ADMIN_MINIO_URL:-}"
admin_dash="${TN_ADMIN_DASHBOARDS_URL:-}"
admin_ai="${TN_ADMIN_AI_URL:-}"

if [ -z "${url}${realm}${client}${admin_kc}${admin_minio}${admin_dash}${admin_ai}" ]; then
  rm -f "$target"
  echo "truenorth: no TN_KEYCLOAK_* / TN_ADMIN_* set; serving the built-in runtime config"
  exit 0
fi

# The values are written into JSON unescaped, so only URL-safe characters are allowed.
# Refusing to start beats serving a config the SPA cannot parse.
check() {
  case "$2" in
    *[!A-Za-z0-9:/._~%-]*)
      echo "truenorth: $1 has characters outside [A-Za-z0-9:/._~%-]; refusing to start" >&2
      exit 1
      ;;
  esac
}
check TN_KEYCLOAK_URL "$url"
check TN_KEYCLOAK_REALM "$realm"
check TN_KEYCLOAK_CLIENT_ID "$client"
check TN_ADMIN_KEYCLOAK_URL "$admin_kc"
check TN_ADMIN_MINIO_URL "$admin_minio"
check TN_ADMIN_DASHBOARDS_URL "$admin_dash"
check TN_ADMIN_AI_URL "$admin_ai"

# object NAME KEY VALUE [KEY VALUE ...]: `"NAME":{...}` with the non-empty pairs, or nothing.
object() {
  name="$1"
  shift
  body=""
  while [ "$#" -ge 2 ]; do
    if [ -n "$2" ]; then
      [ -z "$body" ] || body="${body},"
      body="${body}\"$1\":\"$2\""
    fi
    shift 2
  done
  [ -z "$body" ] || printf '"%s":{%s}' "$name" "$body"
}

kc="$(object keycloak url "$url" realm "$realm" clientId "$client")"
links="$(object adminLinks keycloak "$admin_kc" minio "$admin_minio" dashboards "$admin_dash" ai "$admin_ai")"
sep=""
[ -z "$kc" ] || [ -z "$links" ] || sep=","

printf '{%s%s%s}\n' "$kc" "$sep" "$links" > "$target"
echo "truenorth: runtime config written to $target: $(cat "$target")"
