#!/usr/bin/env bash
# Build the T0 corpus, render the Greyspace stack for it, start it under its own compose
# project, and check the simulated internet end to end from the probe (a stand-in for a
# range's edge router):
#
#   1. DNS: the resolver iterates root -> TLD -> authoritative and answers a fixture domain
#      with the address in the manifest.
#   2. BGP: ISP A has learned ISP B's prefix from its eBGP peer.
#   3. HTTP: a fixture page is served by name, routed across both ISPs to the web farm.
#   4. Threat stub: a threat-actor domain resolves and answers.
#   5. Overlay: a file written to the range overlay is served in front of the corpus.
#
# CI job `greyspace` runs this. Locally: bash greyspace/scripts/check-t0.sh
# Environment: GS_PROJECT (compose project, default gs-t0-check), PY (python, default
# .venv/bin/python or python3), GS_KEEP=1 leaves the stack up.
#
# The stack's network is 198.18.0.0/24 (the first ISP's infrastructure segment), so only
# one Greyspace stack can run per Docker host at a time.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PROJECT="${GS_PROJECT:-gs-t0-check}"
PY="${PY:-}"
if [[ -z "$PY" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then PY="$ROOT/.venv/bin/python"; else PY=python3; fi
fi
OUT="${GS_OUT:-$ROOT/build/greyspace}"
CORPUS="$OUT/corpus-t0"
STACK="$OUT/stack-t0"
COMPOSE=(docker compose -p "$PROJECT" -f "$STACK/compose.yaml" --profile probe)

say() { echo "== greyspace: $*"; }
fail() { echo "greyspace check FAILED: $*" >&2; "${COMPOSE[@]}" logs --no-color --tail 60 >&2 || true; exit 1; }

down() {
  if [[ "${GS_KEEP:-}" != "1" ]]; then
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
}

say "build T0 corpus"
"$PY" greyspace/scripts/corpus.py t0 --out "$CORPUS"
"$PY" greyspace/scripts/corpus.py verify --root "$CORPUS"
say "render stack"
"$PY" greyspace/scripts/corpus.py render --corpus "$CORPUS" --out "$STACK"

trap down EXIT
say "up ($PROJECT)"
"${COMPOSE[@]}" up -d --build --wait --wait-timeout 180

probe() { "${COMPOSE[@]}" exec -T probe "$@"; }

# Expected values come from the manifest, not from this script.
read -r SITE SITE_IP FAR_SITE FAR_IP THREAT THREAT_IP < <("$PY" - "$CORPUS/manifest.json" <<'EOF'
import json, sys
m = json.load(open(sys.argv[1]))
a = next(s for s in m["sites"] if s["ip"].startswith("198.18."))
b = next(s for s in m["sites"] if s["ip"].startswith("198.19."))
t = m["threat_actors"][0]["domains"][0]
print(a["fqdn"], a["ip"], b["fqdn"], b["ip"], t["fqdn"], t["ip"])
EOF
)

say "1. DNS: $SITE via the Greyspace resolver"
got=""
for _ in $(seq 1 20); do
  got="$(probe dig +short +time=2 +tries=1 @198.18.0.53 "$SITE" A | tail -1 || true)"
  [[ "$got" == "$SITE_IP" ]] && break
  sleep 2
done
[[ "$got" == "$SITE_IP" ]] || fail "dig $SITE returned '$got', expected $SITE_IP"
www="$(probe dig +short @198.18.0.53 "www.$SITE" A | tail -1)"
[[ "$www" == "$SITE_IP" ]] || fail "dig www.$SITE returned '$www', expected $SITE_IP"
nx="$(probe dig @198.18.0.53 "no-such-site-$RANDOM.com" A | grep -c NXDOMAIN || true)"
[[ "$nx" -ge 1 ]] || fail "an unknown .com name did not return NXDOMAIN"

say "2. BGP: isp-a learned isp-b's prefix"
learned=""
for _ in $(seq 1 30); do
  learned="$("${COMPOSE[@]}" exec -T isp-a vtysh -c 'show ip bgp' 2>/dev/null | grep -c '198.19.0.0/16' || true)"
  [[ "$learned" -ge 1 ]] && break
  sleep 2
done
[[ "$learned" -ge 1 ]] || fail "isp-a has no BGP route for 198.19.0.0/16"

say "3. HTTP: http://$SITE/ and http://$FAR_SITE/ by name"
body="$(probe curl -fsS --max-time 5 "http://$SITE/")" || fail "GET http://$SITE/ failed"
grep -q "data-gs-fixture=\"$SITE\"" <<<"$body" || fail "http://$SITE/ did not serve the fixture page"
body="$(probe curl -fsS --max-time 5 "http://$FAR_SITE/about.html")" || fail "GET http://$FAR_SITE/about.html failed"
grep -q "fictional" <<<"$body" || fail "http://$FAR_SITE/about.html did not serve the fixture page"
code="$(probe curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$SITE/no-such-page")"
[[ "$code" == "404" ]] || fail "a missing page returned $code, not 404"
VIDEO_URL="$("$PY" -c 'import json,sys; v=json.load(open(sys.argv[1]))["videos"]; print("http://%s/%s" % (v[0]["site"], v[0]["path"].split("/", 2)[2]) if v else "")' "$CORPUS/manifest.json")"
if [[ -n "$VIDEO_URL" ]]; then
  ctype="$(probe curl -fsS -o /dev/null -w '%{content_type}' --max-time 5 "$VIDEO_URL")" || fail "GET $VIDEO_URL failed"
  [[ "$ctype" == video/* ]] || fail "$VIDEO_URL served as '$ctype', not video"
fi

say "4. Threat stub: $THREAT"
got="$(probe dig +short @198.18.0.53 "$THREAT" A | tail -1)"
[[ "$got" == "$THREAT_IP" ]] || fail "dig $THREAT returned '$got', expected $THREAT_IP"
probe curl -fsS --max-time 5 "http://$THREAT/" | grep -q 'data-gs-threat' || fail "the threat stub did not answer"

say "5. Overlay: a breadcrumb in front of the read-only corpus"
"${COMPOSE[@]}" exec -T webfarm sh -c \
  "mkdir -p /srv/overlay/sites/$SITE && echo 'gs-breadcrumb-check' > /srv/overlay/sites/$SITE/crumb.txt"
probe curl -fsS --max-time 5 "http://$SITE/crumb.txt" | grep -q gs-breadcrumb-check || fail "overlay file not served"
if "${COMPOSE[@]}" exec -T webfarm sh -c "touch /srv/corpus/x" 2>/dev/null; then
  fail "the corpus mount is writable"
fi

say "PASS"
