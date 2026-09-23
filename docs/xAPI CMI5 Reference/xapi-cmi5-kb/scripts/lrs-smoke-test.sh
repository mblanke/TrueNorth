#!/usr/bin/env bash
# lrs-smoke-test.sh - functional smoke test of an xAPI LRS (1.0.3 or 2.0.0)
#
# Where to run: any host that can reach the LRS. Needs bash, curl, jq, base64 (uuidgen optional).
# Usage:
#   export LRS_ENDPOINT=http://127.0.0.1:8080/xapi LRS_KEY=... LRS_SECRET=...
#   XAPI_VERSION=1.0.3 ./lrs-smoke-test.sh      # default 1.0.3; use 2.0.0 for 2.0
# Exit code: 0 if all checks pass, 1 otherwise.
# Side effects: stores 1 statement (not voided), creates then deletes 1 State and 1 Activity Profile document.

set -uo pipefail
: "${LRS_ENDPOINT:?set LRS_ENDPOINT (e.g. http://127.0.0.1:8080/xapi)}"
: "${LRS_KEY:?set LRS_KEY}"; : "${LRS_SECRET:?set LRS_SECRET}"
V="${XAPI_VERSION:-1.0.3}"
EP="${LRS_ENDPOINT%/}"
AUTH="Basic $(printf '%s:%s' "$LRS_KEY" "$LRS_SECRET" | base64 | tr -d '\n')"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
FAILS=0

enc() { jq -rn --arg v "$1" '$v|@uri'; }
uuid() { if command -v uuidgen >/dev/null; then uuidgen | tr 'A-Z' 'a-z';
         elif [[ -r /proc/sys/kernel/random/uuid ]]; then cat /proc/sys/kernel/random/uuid;
         else python3 -c 'import uuid;print(uuid.uuid4())'; fi; }
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAILS=$((FAILS+1)); }

# req METHOD URL [BODY] [extra curl args...] -> sets CODE, body in $TMP/body, headers in $TMP/hdr
req() {
  local m="$1" u="$2" b="${3:-}"; shift 3 2>/dev/null || shift $#
  local args=(-s -X "$m" -o "$TMP/body" -D "$TMP/hdr" -w '%{http_code}'
              -H "Authorization: $AUTH" -H "X-Experience-API-Version: $V")
  if [[ -n "$b" ]]; then args+=(-H 'Content-Type: application/json' --data-binary "$b"); fi
  CODE="$(curl "${args[@]}" "$@" "$u")" || CODE="000"
}
hdr() { grep -i "^$1:" "$TMP/hdr" | head -1 | cut -d: -f2- | tr -d '\r' | sed 's/^ *//'; }

echo "LRS: $EP  xAPI version header: $V"

# 1. About
req GET "$EP/about"
if [[ "$CODE" == 200 ]] && jq -e --arg v "$V" '.version | index($v)' "$TMP/body" >/dev/null 2>&1; then
  pass "GET /about lists version $V ($(jq -c .version "$TMP/body"))"
else fail "GET /about (HTTP $CODE): $(head -c 300 "$TMP/body")"; fi

# 2. Store a statement with a client-generated id
SID="$(uuid)"
ACTOR='{"objectType":"Agent","account":{"homePage":"https://id.example.org","name":"smoke-test-agent"}}'
ACT="https://vocab.example.org/xapi/activities/smoke-test"
STMT="$(jq -nc --arg id "$SID" --argjson actor "$ACTOR" --arg act "$ACT" \
  --arg ts "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" '{
  id:$id, actor:$actor,
  verb:{id:"http://adlnet.gov/expapi/verbs/experienced",display:{"en-CA":"experienced"}},
  object:{objectType:"Activity",id:$act,definition:{name:{"en-CA":"LRS smoke test"}}},
  timestamp:$ts}')"
req POST "$EP/statements" "$STMT"
if [[ "$CODE" == 200 ]] && jq -e --arg id "$SID" 'index($id)' "$TMP/body" >/dev/null 2>&1; then
  pass "POST /statements returned id $SID"
else fail "POST /statements (HTTP $CODE): $(head -c 300 "$TMP/body")"; fi

# 3. Read it back
req GET "$EP/statements?statementId=$SID"
if [[ "$CODE" == 200 ]] && [[ "$(jq -r .id "$TMP/body")" == "$SID" ]]; then
  pass "GET statementId returns stored statement (stored=$(jq -r .stored "$TMP/body"))"
else fail "GET statementId (HTTP $CODE)"; fi

# 4. Idempotent re-send of identical statement
req POST "$EP/statements" "$STMT"
if [[ "$CODE" == 200 || "$CODE" == 204 ]]; then pass "Re-POST identical statement accepted (HTTP $CODE)"
else fail "Re-POST identical statement (HTTP $CODE): $(head -c 200 "$TMP/body")"; fi

# 5. Same id, different content must conflict
STMT2="$(jq -c '.verb.id="http://adlnet.gov/expapi/verbs/attempted"' <<<"$STMT")"
req PUT "$EP/statements?statementId=$SID" "$STMT2"
if [[ "$CODE" == 409 ]]; then pass "PUT same id with different content -> 409 Conflict"
else fail "PUT conflicting statement expected 409, got $CODE"; fi

# 6. State document round trip
Q="activityId=$(enc "$ACT")&agent=$(enc "$ACTOR")&stateId=smoke-state"
req PUT "$EP/activities/state?$Q" '{"bookmark":"page-3"}'
if [[ "$CODE" == 204 ]]; then pass "PUT State document"; else fail "PUT State (HTTP $CODE): $(head -c 200 "$TMP/body")"; fi
req GET "$EP/activities/state?$Q"
if [[ "$CODE" == 200 ]] && [[ "$(jq -r .bookmark "$TMP/body" 2>/dev/null)" == "page-3" ]]; then pass "GET State document matches"
else fail "GET State (HTTP $CODE)"; fi
req GET "$EP/activities/state?$Q"; ET="$(hdr ETag)"
DEL_ARGS=(); [[ -n "$ET" ]] && DEL_ARGS=(-H "If-Match: $ET")
req DELETE "$EP/activities/state?$Q" "" ${DEL_ARGS[@]+"${DEL_ARGS[@]}"}
[[ "$CODE" == 204 ]] && pass "DELETE State document" || fail "DELETE State (HTTP $CODE)"

# 7. Activity Profile concurrency (ETag / If-Match / If-None-Match)
PQ="activityId=$(enc "$ACT")&profileId=smoke-profile-$(uuid | cut -c1-8)"
req PUT "$EP/activities/profile?$PQ" '{"rev":1}' -H 'If-None-Match: *'
[[ "$CODE" == 204 ]] && pass "PUT Activity Profile with If-None-Match: * (create)" || fail "Create profile doc (HTTP $CODE): $(head -c 200 "$TMP/body")"
req GET "$EP/activities/profile?$PQ"; ET1="$(hdr ETag)"
[[ -n "$ET1" ]] && pass "GET Activity Profile returns ETag $ET1" || fail "No ETag on Activity Profile GET"
req PUT "$EP/activities/profile?$PQ" '{"rev":2}' -H "If-Match: $ET1"
[[ "$CODE" == 204 ]] && pass "PUT with current If-Match (update)" || fail "Update with If-Match (HTTP $CODE)"
req PUT "$EP/activities/profile?$PQ" '{"rev":3}' -H "If-Match: $ET1"
[[ "$CODE" == 412 ]] && pass "PUT with stale If-Match -> 412 Precondition Failed" || fail "Stale ETag expected 412, got $CODE"
req GET "$EP/activities/profile?$PQ"; ET2="$(hdr ETag)"
req DELETE "$EP/activities/profile?$PQ" "" -H "If-Match: $ET2"
[[ "$CODE" == 204 ]] && pass "DELETE Activity Profile with If-Match" || fail "DELETE profile (HTTP $CODE)"

# 8. Filtered query
req GET "$EP/statements?verb=$(enc http://adlnet.gov/expapi/verbs/experienced)&activity=$(enc "$ACT")&limit=5"
if [[ "$CODE" == 200 ]] && jq -e '.statements | type=="array"' "$TMP/body" >/dev/null 2>&1; then
  pass "Filtered GET /statements returns StatementResult ($(jq '.statements|length' "$TMP/body") statements; consistent-through: $(hdr X-Experience-API-Consistent-Through))"
else fail "Filtered query (HTTP $CODE)"; fi

echo
if (( FAILS == 0 )); then echo "RESULT: all checks passed"; exit 0; else echo "RESULT: $FAILS check(s) failed"; exit 1; fi
