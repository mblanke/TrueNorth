#!/usr/bin/env bash
# cmi5-session-sim.sh - drive one complete cmi5 (Quartz) Normal-mode session through an LRS with curl,
# playing both roles so every artefact can be inspected:
#   LMS: write LMS.LaunchData + cmi5LearnerPreferences, record "launched", build the launch URL
#   AU : read launch data + preferences, send initialized, progressed (cmi5-allowed), completed, passed, terminated
#   LMS: record "satisfied" for the course (this simulated course has a single AU)
# Then query by registration and print the verb chain.
#
# Where to run: any host that can reach the LRS. Needs bash, curl, jq, base64, GNU date.
# Usage:
#   export LRS_ENDPOINT=http://127.0.0.1:8080/xapi LRS_KEY=... LRS_SECRET=...
#   ./cmi5-session-sim.sh              # live
#   DRY_RUN=1 ./cmi5-session-sim.sh    # print requests only (credentials redacted), no LRS needed
#   LMS_ONLY=1 FETCH_URL=http://127.0.0.1:8099/fetch ./cmi5-session-sim.sh
#                                      # LMS launch steps only; prints the launch URL for a real AU (see au/)
# Simplification: a real LMS mints a session-scoped credential and returns it from the one-time fetch URL.
# Here the "auth-token" is simply the Basic credential for LRS_KEY:LRS_SECRET.

set -euo pipefail
DRY="${DRY_RUN:-0}"; LMS_ONLY="${LMS_ONLY:-0}"
EP="${LRS_ENDPOINT:-http://127.0.0.1:8080/xapi}"; EP="${EP%/}"
if [[ "$DRY" != 1 ]]; then : "${LRS_KEY:?set LRS_KEY}"; : "${LRS_SECRET:?set LRS_SECRET}"; fi
V="1.0.3"   # cmi5 Quartz is bound to xAPI 1.0.3
TOKEN="$(printf '%s:%s' "${LRS_KEY:-dry}" "${LRS_SECRET:-run}" | base64 | tr -d '\n')"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

uuid() { if command -v uuidgen >/dev/null; then uuidgen | tr 'A-Z' 'a-z';
         elif [[ -r /proc/sys/kernel/random/uuid ]]; then cat /proc/sys/kernel/random/uuid;
         else python3 -c 'import uuid;print(uuid.uuid4())'; fi; }
enc()  { jq -rn --arg v "$1" '$v|@uri'; }
now()  { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
ms()   { date +%s%3N; }
dur()  { local d=$(( $1 < 0 ? 0 : $1 )); printf 'PT%d.%02dS' $((d/1000)) $(((d%1000)/10)); }  # ISO 8601, 0.01 s precision

# api EXPECTED_CODE METHOD PATH_AND_QUERY [BODY] [extra headers...]
api() {
  local want="$1" m="$2" path="$3" body="${4:-}"; shift 4 2>/dev/null || shift $#
  if [[ "$DRY" == 1 ]]; then
    printf '\n>>> %s %s/%s\n    Authorization: Basic <redacted>\n    X-Experience-API-Version: %s\n' "$m" "$EP" "$path" "$V"
    for h in "$@"; do [[ "$h" != -H ]] && printf '    %s\n' "$h"; done
    [[ -n "$body" ]] && jq . <<<"$body" | sed 's/^/    /'
    return 0
  fi
  local args=(-s -X "$m" -o "$TMP/body" -w '%{http_code}' -H "Authorization: Basic $TOKEN" -H "X-Experience-API-Version: $V")
  [[ -n "$body" ]] && args+=(-H 'Content-Type: application/json' --data-binary "$body")
  local code; code="$(curl "${args[@]}" "$@" "$EP/$path")" || code=000
  if [[ "$code" != "$want" ]]; then
    echo "ERROR: $m $path -> HTTP $code (expected $want): $(head -c 400 "$TMP/body" 2>/dev/null)" >&2; exit 1
  fi
  printf '%-7s %-60.60s %s\n' "$m" "$path" "$code"
}

# ---------- identifiers ----------
REG="$(uuid)"; SESSION="$(uuid)"; SHORT="$(uuid | cut -c1-8)"
ACTOR="$(jq -nc --arg n "sim-learner-$SHORT" '{objectType:"Agent",account:{homePage:"https://lms.example.org",name:$n}}')"
AU_ID="https://lms.example.org/xapi/activities/au/$SHORT"                 # LMS-generated runtime activity ID
AU_PUB="https://content.example.org/cmi5/cyber-hygiene-101/au/module-1"   # publisher ID from cmi5.xml
COURSE_ID="https://lms.example.org/xapi/activities/course/$SHORT"
COURSE_PUB="https://content.example.org/cmi5/cyber-hygiene-101"
AU_URL="https://content.example.org/cmi5/cyber-hygiene-101/module-1/index.html"
FETCH_URL="${FETCH_URL:-https://lms.example.org/cmi5/fetch/$(uuid)}"
MASTERY=0.8; MOVEON="CompletedAndPassed"; LAUNCH_PARAMS='{"scenario":"phishing-a","lang":"en-CA"}'
X="https://w3id.org/xapi/cmi5/context/extensions"
CAT_CMI5='{"id":"https://w3id.org/xapi/cmi5/context/categories/cmi5"}'
CAT_MOVEON='{"id":"https://w3id.org/xapi/cmi5/context/categories/moveon"}'
echo "registration=$REG session=$SESSION actor=$(jq -r .account.name <<<"$ACTOR")"

STATE_Q="activityId=$(enc "$AU_ID")&agent=$(enc "$ACTOR")&registration=$REG&stateId=LMS.LaunchData"
PREFS_Q="agent=$(enc "$ACTOR")&profileId=cmi5LearnerPreferences"

# ---------- LMS: launch data, preferences, launched ----------
echo "== LMS: prepare launch"
LAUNCHDATA="$(jq -nc --arg pub "$AU_PUB" --arg sid "$SESSION" --arg x "$X" --argjson ms "$MASTERY" \
  --arg mo "$MOVEON" --arg lp "$LAUNCH_PARAMS" '{
  contextTemplate:{contextActivities:{grouping:[{objectType:"Activity",id:$pub}]},extensions:{($x+"/sessionid"):$sid}},
  launchMode:"Normal", launchParameters:$lp, masteryScore:$ms, moveOn:$mo,
  returnURL:"https://lms.example.org/course/outline"}')"
api 204 PUT "activities/state?$STATE_Q" "$LAUNCHDATA"
api 204 PUT "agents/profile?$PREFS_Q" '{"languagePreference":"en-CA,fr-CA","audioPreference":"on"}' -H 'If-None-Match: *'

stmt() {  # stmt VERB_IRI DISPLAY OBJECT_JSON CONTEXT_JSON [RESULT_JSON]
  jq -nc --arg id "$(uuid)" --argjson actor "$ACTOR" --arg v "$1" --arg d "$2" --argjson obj "$3" \
    --argjson ctx "$4" --argjson res "${5:-null}" --arg ts "$(now)" '
    {id:$id,actor:$actor,verb:{id:$v,display:{"en-US":$d}},object:$obj,context:$ctx,timestamp:$ts}
    + (if $res then {result:$res} else {} end)'
}
AU_OBJ="$(jq -nc --arg id "$AU_ID" '{objectType:"Activity",id:$id}')"
LMS_CTX="$(jq -nc --arg reg "$REG" --arg pub "$AU_PUB" --arg sid "$SESSION" --arg x "$X" --argjson c "$CAT_CMI5" \
  --arg mode Normal --arg url "$AU_URL" --arg mo "$MOVEON" --argjson ms "$MASTERY" --arg lp "$LAUNCH_PARAMS" '{
  registration:$reg, contextActivities:{category:[$c],grouping:[{objectType:"Activity",id:$pub}]},
  extensions:{($x+"/sessionid"):$sid,($x+"/launchmode"):$mode,($x+"/launchurl"):$url,($x+"/moveon"):$mo,
              ($x+"/masteryscore"):$ms,($x+"/launchparameters"):$lp}}')"
api 200 POST statements "$(stmt http://adlnet.gov/expapi/verbs/launched launched "$AU_OBJ" "$LMS_CTX")"

LAUNCH_URL="$AU_URL?endpoint=$(enc "$EP/")&fetch=$(enc "$FETCH_URL")&actor=$(enc "$ACTOR")&registration=$REG&activityId=$(enc "$AU_ID")"
echo "Launch URL:"; echo "  $LAUNCH_URL"
if [[ "$LMS_ONLY" == 1 ]]; then
  printf 'REGISTRATION=%q\nLAUNCH_QUERY=%q\n' "$REG" "${LAUNCH_URL#*\?}" > "${LMS_ONLY_OUT:-/dev/stdout}"; exit 0
fi

# ---------- AU ----------
echo "== AU: fetch token (simulated), read launch data and preferences"
if [[ "$DRY" == 1 ]]; then
  printf '\n>>> POST %s  (one-time)\n<<< 200 {"auth-token":"<redacted>"}\n' "$FETCH_URL"; LD="$LAUNCHDATA"
else
  api 200 GET "activities/state?$STATE_Q"; LD="$(cat "$TMP/body")"
  api 200 GET "agents/profile?$PREFS_Q"; echo "        learner prefers: $(jq -r .languagePreference "$TMP/body"), audio $(jq -r .audioPreference "$TMP/body")"
fi
TEMPLATE="$(jq -c .contextTemplate <<<"$LD")"
[[ "$(jq -r --arg k "$X/sessionid" '.extensions[$k]' <<<"$TEMPLATE")" == "$SESSION" ]] || { echo "ERROR: sessionid missing from contextTemplate" >&2; exit 1; }
[[ "$(jq -r .launchMode <<<"$LD")" == Normal ]] || { echo "Not Normal mode: AU must not send completed/passed/failed" >&2; exit 1; }
MS_LD="$(jq -r .masteryScore <<<"$LD")"

ctx() {  # ctx CATEGORIES_JSON_ARRAY_OR_null [EXTRA_EXTENSIONS_JSON]
  jq -nc --argjson t "$TEMPLATE" --arg reg "$REG" --argjson cats "$1" --argjson ext "${2:-{\}}" '
    $t + {registration:$reg}
    | (if $cats then .contextActivities.category = $cats else . end)
    | .extensions += $ext'
}
echo "== AU: session statements"
T0="$(ms)"
api 200 POST statements "$(stmt http://adlnet.gov/expapi/verbs/initialized initialized "$AU_OBJ" "$(ctx "[$CAT_CMI5]")")"
sleep 0.3
api 200 POST statements "$(stmt http://adlnet.gov/expapi/verbs/progressed progressed "$AU_OBJ" "$(ctx null)" \
  '{"extensions":{"https://w3id.org/xapi/cmi5/result/extensions/progress":50}}')"
sleep 0.3
api 200 POST statements "$(stmt http://adlnet.gov/expapi/verbs/completed completed "$AU_OBJ" "$(ctx "[$CAT_CMI5,$CAT_MOVEON]")" \
  "$(jq -nc --arg d "$(dur $(( $(ms) - T0 )))" '{completion:true,duration:$d}')")"
SCALED=0.9
awk -v s="$SCALED" -v m="$MS_LD" 'BEGIN{exit !(s>=m)}' || { echo "score below masteryScore: would send failed" >&2; exit 1; }
api 200 POST statements "$(stmt http://adlnet.gov/expapi/verbs/passed passed "$AU_OBJ" \
  "$(ctx "[$CAT_CMI5,$CAT_MOVEON]" "$(jq -nc --arg k "$X/masteryscore" --argjson m "$MS_LD" '{($k):$m}')")" \
  "$(jq -nc --arg d "$(dur $(( $(ms) - T0 )))" --argjson s "$SCALED" '{success:true,score:{scaled:$s,raw:18,min:0,max:20},duration:$d}')")"
sleep 0.3
api 200 POST statements "$(stmt http://adlnet.gov/expapi/verbs/terminated terminated "$AU_OBJ" "$(ctx "[$CAT_CMI5]")" \
  "$(jq -nc --arg d "$(dur $(( $(ms) - T0 )))" '{duration:$d}')")"
echo "        AU would now redirect to returnURL: $(jq -r .returnURL <<<"$LD")"

# ---------- LMS: satisfaction ----------
echo "== LMS: evaluate moveOn ($MOVEON met) and satisfy course"
COURSE_OBJ="$(jq -nc --arg id "$COURSE_ID" '{objectType:"Activity",id:$id,definition:{type:"https://w3id.org/xapi/cmi5/activitytype/course"}}')"
SAT_CTX="$(jq -nc --arg reg "$REG" --arg pub "$COURSE_PUB" --arg sid "$SESSION" --arg x "$X" --argjson c "$CAT_CMI5" '{
  registration:$reg, contextActivities:{category:[$c],grouping:[{objectType:"Activity",id:$pub}]},extensions:{($x+"/sessionid"):$sid}}')"
api 200 POST statements "$(stmt https://w3id.org/xapi/adl/verbs/satisfied satisfied "$COURSE_OBJ" "$SAT_CTX")"

# ---------- verify ----------
echo "== Verify: statements for registration $REG"
if [[ "$DRY" == 1 ]]; then echo "(dry run) expected: launched -> initialized -> progressed -> completed -> passed -> terminated -> satisfied"; exit 0; fi
api 200 GET "statements?registration=$REG&ascending=true&limit=50"
CHAIN="$(jq -r '[.statements | sort_by(.timestamp)[] | .verb.display["en-US"]] | join(" -> ")' "$TMP/body")"
echo "$CHAIN"
[[ "$CHAIN" == "launched -> initialized -> progressed -> completed -> passed -> terminated -> satisfied" ]] \
  && echo "RESULT: session recorded as expected" || { echo "RESULT: unexpected sequence" >&2; exit 1; }
