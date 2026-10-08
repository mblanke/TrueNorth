#!/bin/sh
# Writes the SPA's runtime config (assets/config.json) from the container environment,
# so one web image serves every deployment. Run by the nginx image's
# /docker-entrypoint.sh before nginx starts (any *.sh in /docker-entrypoint.d/).
#
#   TN_KEYCLOAK_URL        browser-facing Keycloak base URL   (build default: /auth)
#   TN_KEYCLOAK_REALM      realm                              (build default: truenorth)
#   TN_KEYCLOAK_CLIENT_ID  public OIDC client                 (build default: truenorth-web)
#
# None set: the image's own config.json ({}) is left alone and the build defaults in
# environment.prod.ts apply. There is deliberately no variable for disabling auth: the
# SPA ignores everything in this file except the Keycloak endpoint.
set -eu

target="${TN_RUNTIME_CONFIG_PATH:-/usr/share/nginx/html/assets/config.json}"
url="${TN_KEYCLOAK_URL:-}"
realm="${TN_KEYCLOAK_REALM:-}"
client="${TN_KEYCLOAK_CLIENT_ID:-}"

if [ -z "${url}${realm}${client}" ]; then
  echo "truenorth: no TN_KEYCLOAK_* set; serving the built-in runtime config"
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

fields=""
add() {
  [ -n "$2" ] || return 0
  [ -z "$fields" ] || fields="${fields},"
  fields="${fields}\"$1\":\"$2\""
}
add url "$url"
add realm "$realm"
add clientId "$client"

printf '{"keycloak":{%s}}\n' "$fields" > "$target"
echo "truenorth: runtime config written to $target: $(cat "$target")"
