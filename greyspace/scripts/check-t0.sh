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
#   6. HTTPS: a site answers over TLS with a certificate from the Greyspace root CA, which
#      is published at http://pki.gs-infra.net/root.crt.
#   7. Mail: MX for a site zone points at the mail service; a message sent over SMTP shows
#      in the webmail API.
#   8. NTP: ntp.gs-infra.net serves time.
#   9. NPC traffic: the simulated users (profile office-day, sped up) browse the web farm
#      (their User-Agent is in its access log) and send mail.
#  10. Breadcrumbs: the greyspace_breadcrumb injector's payload, planted with bin/gs (the
#      same command a gs-core VM runs), is served (web, DNS TXT, threat feed), passes
#      `gs crumb check`, and is gone again after `gs crumb remove`.
#  11. Corpus ingest: a sample WARC -> corpus tree + manifest -> verify -> report.
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
"$PY" greyspace/scripts/corpus.py render --corpus "$CORPUS" --out "$STACK" --npc office-day --project "$PROJECT"

trap down EXIT
say "up ($PROJECT)"
export GS_NPC_SPEED="${GS_NPC_SPEED:-20}"
"${COMPOSE[@]}" up -d --build --wait --wait-timeout 240

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

say "6. HTTPS: https://$SITE/ with the Greyspace root CA"
probe curl -fsS --max-time 5 "http://pki.gs-infra.net/root.crt" | grep -q 'BEGIN CERTIFICATE' \
  || fail "the CA certificate is not published at http://pki.gs-infra.net/root.crt"
probe curl -fsS --max-time 5 --cacert /certs/root.crt "https://$SITE/" | grep -q "data-gs-fixture=\"$SITE\"" \
  || fail "https://$SITE/ did not verify against the Greyspace CA or did not serve the page"
probe curl -fsS --max-time 5 --cacert /certs/root.crt "https://www.$SITE/about.html" >/dev/null \
  || fail "https://www.$SITE/ (the zone certificate's wildcard) did not verify"
if probe curl -fsS --max-time 5 "https://$SITE/" >/dev/null 2>&1; then
  fail "https://$SITE/ verified without the Greyspace CA (it must not chain to a public root)"
fi

read -r MAILZONE < <("$PY" -c 'import json,sys; print(next(s["fqdn"] for s in json.load(open(sys.argv[1]))["sites"] if s["category"] == "webmail"))' "$CORPUS/manifest.json")
say "7. Mail: MX $MAILZONE, SMTP to mail.gs-infra.net, webmail API"
mx="$(probe dig +short @198.18.0.53 MX "$MAILZONE" | tail -1)"
[[ "$mx" == "10 mail.gs-infra.net." ]] || fail "MX $MAILZONE returned '$mx'"
probe sh -c "printf 'Subject: gs-t0-mail\r\nFrom: probe@$SITE\r\nTo: check@$MAILZONE\r\n\r\nhello from the probe\r\n' > /tmp/m && \
  curl -fsS --max-time 10 --url smtp://mail.gs-infra.net:25 --mail-from probe@$SITE --mail-rcpt check@$MAILZONE -T /tmp/m" \
  || fail "SMTP to mail.gs-infra.net failed"
got=""
for _ in $(seq 1 10); do
  got="$(probe curl -fsS --max-time 5 "http://webmail.$MAILZONE/api/v1/messages?limit=200" | grep -c 'gs-t0-mail' || true)"
  [[ "$got" -ge 1 ]] && break
  sleep 1
done
[[ "$got" -ge 1 ]] || fail "the message sent over SMTP is not in webmail.$MAILZONE"

say "8. NTP: ntp.gs-infra.net"
probe chronyd -Q -t 20 'server ntp.gs-infra.net iburst maxsamples 2' >/dev/null 2>&1 \
  || fail "no time from ntp.gs-infra.net"

say "9. NPC traffic (office-day x$GS_NPC_SPEED)"
hits=0; mails=0
for _ in $(seq 1 45); do
  hits="$("${COMPOSE[@]}" logs --no-color webfarm 2>/dev/null | grep -c 'GreyspaceNPC/1' || true)"
  mails="$("${COMPOSE[@]}" logs --no-color npc 2>/dev/null | grep '"action": "mail"' | grep -c '"ok": true' || true)"
  [[ "$hits" -ge 10 && "$mails" -ge 1 ]] && break
  sleep 2
done
[[ "$hits" -ge 10 ]] || fail "only $hits NPC requests reached the web farm"
[[ "$mails" -ge 1 ]] || fail "the NPCs sent no mail"
echo "   $hits NPC requests in the web farm log, $mails NPC messages sent"

say "10. Breadcrumbs: plant, serve, check, remove"
GS=(env GS_PROJECT="$PROJECT" "$PY" "$STACK/bin/gs")
TOKENS="$("$PY" greyspace/scripts/crumb_payload.py --t0 "$SITE" --exercise t0-check --out "$OUT/crumbs.json")"
TOKEN_NOTE="$(grep '^note=' <<<"$TOKENS" | cut -d= -f2)"
TOKEN_TXT="$(grep '^txt=' <<<"$TOKENS" | cut -d= -f2)"
ZONE="$(awk -F. '{print $(NF-1)"."$NF}' <<<"$SITE")"
"${GS[@]}" crumb plant --file "$OUT/crumbs.json" >/dev/null || fail "gs crumb plant failed"
probe curl -fsS --max-time 5 "http://$SITE/notes/handover.txt" | grep -q "$TOKEN_NOTE" || fail "the web breadcrumb is not served"
txt=""
for _ in $(seq 1 15); do  # CoreDNS reloads the zone within 5 s; the resolver keeps an NXDOMAIN 5 s at most
  txt="$(probe dig +short @198.18.0.53 TXT "crumb.$ZONE" | tr -d '"')"
  [[ "$txt" == "$TOKEN_TXT" ]] && break
  sleep 2
done
[[ "$txt" == "$TOKEN_TXT" ]] || fail "TXT crumb.$ZONE returned '$txt', expected $TOKEN_TXT"
probe curl -fsS --max-time 5 "http://intel.gs-infra.net/feed.txt" | grep -q '^domain,update-cdn-sync.net,AMBER HERON' \
  || fail "the threat-feed breadcrumb is not in http://intel.gs-infra.net/feed.txt"
"${GS[@]}" crumb check --exercise t0-check >/dev/null || fail "gs crumb check did not find every breadcrumb"
"${GS[@]}" crumb remove --exercise t0-check >/dev/null || fail "gs crumb remove failed"
code="$(probe curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$SITE/notes/handover.txt")"
[[ "$code" == "404" ]] || fail "the web breadcrumb is still served after remove ($code)"
gone=""
for _ in $(seq 1 15); do
  gone="$(probe dig +short @198.18.0.12 TXT "crumb.$ZONE")"
  [[ -z "$gone" ]] && break
  sleep 2
done
[[ -z "$gone" ]] || fail "the TXT breadcrumb is still in the authoritative zone after remove"
if probe curl -fsS --max-time 5 "http://intel.gs-infra.net/feed.txt" | grep -q 'update-cdn-sync.net'; then
  fail "the threat-feed breadcrumb is still in the feed after remove"
fi
"${GS[@]}" health --json >/dev/null || fail "gs health reports a problem"

say "11. Corpus ingest: sample WARC -> tree -> manifest -> verify -> report"
"$PY" greyspace/scripts/corpus.py sample-warc --out "$OUT/sample.warc.gz"
"$PY" greyspace/scripts/corpus.py ingest --warc "$OUT/sample.warc.gz" --out "$OUT/corpus-ingested" --version t0-ingest
"$PY" greyspace/scripts/corpus.py verify --root "$OUT/corpus-ingested"
"$PY" greyspace/scripts/corpus.py report --root "$OUT/corpus-ingested" --out "$OUT/corpus-report.json"
"$PY" "$STACK/bin/gs" corpus verify "$OUT/corpus-ingested" >/dev/null || fail "gs corpus verify rejected the ingested corpus"

say "PASS"
