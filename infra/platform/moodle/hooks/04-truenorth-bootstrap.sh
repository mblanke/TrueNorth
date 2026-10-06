#!/bin/sh
#
# Wire this Moodle to TrueNorth on every start (after install/upgrade in 02):
# sign-in and sync key, LTI tool, login redirect, TrueNorth branding.
#
# Environment:
#   TN_TOOL_URL        where students' browsers reach the TrueNorth API, e.g.
#                      https://range.example/api. Unset: nothing is wired (plain Moodle).
#   TN_PUBLIC_KEY_URL  where this node fetches TrueNorth's public key, e.g.
#                      http://api:8080/lti/public-key.pem. Fetched each start, so a
#                      key rotation arrives with a restart; the last good copy is kept
#                      if TrueNorth is unreachable.
#   TN_PUBLIC_KEY      the key itself (PEM), instead of TN_PUBLIC_KEY_URL.
#   TN_LOGIN_URL       send Moodle's login page here (the TrueNorth app). Optional.
#   TN_TENANT_ID       the TrueNorth tenant this node serves; course sync is refused
#                      until it is set (a sync ticket must name this tenant).
#
# Writes /var/www/moodledata/truenorth-registration.json: the values TrueNorth's
# platform registration needs (scripts/moodle-farm.sh reads it).
set -eu

if [ -z "${TN_TOOL_URL:-}" ]; then
    echo "[truenorth] TN_TOOL_URL not set; not wiring this Moodle to TrueNorth"
    exit 0
fi

data=/var/www/moodledata
pem="$data/truenorth-tool-public.pem"
if [ -n "${TN_PUBLIC_KEY:-}" ]; then
    printf '%s\n' "$TN_PUBLIC_KEY" > "$pem"
elif [ -n "${TN_PUBLIC_KEY_URL:-}" ]; then
    if wget -q -T 10 -O "$pem.new" "$TN_PUBLIC_KEY_URL" && grep -q "BEGIN PUBLIC KEY" "$pem.new"; then
        mv "$pem.new" "$pem"
    else
        rm -f "$pem.new"
        echo "[truenorth] WARNING: could not fetch $TN_PUBLIC_KEY_URL; keeping the last key" >&2
    fi
fi
if ! grep -q "BEGIN PUBLIC KEY" "$pem" 2>/dev/null; then
    echo "[truenorth] ERROR: no TrueNorth public key (set TN_PUBLIC_KEY_URL or TN_PUBLIC_KEY)" >&2
    exit 1
fi

set -- --sso-publickey="$pem"
[ -n "${TN_TENANT_ID:-}" ] && set -- "$@" --tenant-id="$TN_TENANT_ID"
[ -n "${TN_LOGIN_URL:-}" ] && set -- "$@" --tn-login-url="$TN_LOGIN_URL"
[ -f /opt/truenorth/truenorth.scss ] && set -- "$@" --theme-scss=/opt/truenorth/truenorth.scss
php /opt/truenorth/bootstrap/truenorth_setup.php "$@"
php /opt/truenorth/bootstrap/truenorth_lti_tool.php --tool="$TN_TOOL_URL" --publickey="$pem" \
    > "$data/truenorth-registration.json"
echo "[truenorth] wired to $TN_TOOL_URL"
