#!/usr/bin/env bash
# TrueNorth work site - tidy TrueNorth's objects in vCenter: one TrueNorth folder tree and
# tn-* tags (ADR 0012 follow-up). Idempotent; run it on TN-MGMT01 once the templates are built.
#
#   sudo scripts/lab/vcenter-organize.sh --dry-run     # read-only: print the plan
#   sudo scripts/lab/vcenter-organize.sh               # print the plan, ask y/N, apply it
#
# What it does (details: scripts/lab/vcenter_organize.py):
#   - folders TrueNorth/Templates, TrueNorth/Templates/_retired, TrueNorth/Ranges
#   - TrueNorth's VMs only, out of the old truenorth/ tree: catalogue templates and copies to
#     TrueNorth/Templates; templates TrueNorth registered (or --retire NAME) to _retired (never
#     powered-on ones, never deleted); range folders to TrueNorth/Ranges/<range-name>-<key>.
#     Anything else there is reported and left.
#   - an old truenorth folder only when this run emptied it, nothing failed, and vCenter
#     reports no children at that moment
#   - tags tn-type / tn-os / tn-template / tn-range on TrueNorth's VMs only
# Moving a VM between folders does not touch it (running VMs keep running). Apply re-reads
# the ranges itself and refuses unless every range is settled (ready, running, stopped,
# failed, destroyed), and unless the running worker builds into TrueNorth/Ranges.
#
# Options:
#   --dry-run        print the plan; change nothing
#   --yes            do not ask
#   --retire NAME    also retire this template from truenorth/ (repeatable), e.g. kali-linux
#   --no-db          do not read the TrueNorth ranges (folders keep their names, and nothing
#                    checks for a range being built: be sure none is)
#   --ranges FILE    the ranges as JSON instead (scripts/lab/tn_ranges_inprocess.py output)
#   --force-folder   apply although the worker's VSPHERE_RANGE_FOLDER is not (or cannot be
#                    confirmed to be) TrueNorth/Ranges
#   --env-file FILE  vCenter credentials (default /srv/truenorth/config/.env.production)
#   --app-dir DIR    installed app (compose file), default /srv/truenorth/app
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
REPO="$(cd "$LAB_DIR/../.." && pwd)"
ENV_FILE="${TN_ENV_FILE:-/srv/truenorth/config/.env.production}"
APP_DIR="${TN_APP_DIR:-/srv/truenorth/app}"
PY="${TN_LAB_PYTHON:-python3}"
TARGET_FOLDER="TrueNorth/Ranges"
WORKER_SERVICE="${TN_WORKER_SERVICE:-worker-provision}"   # the compose service that builds ranges
DRY=0; NO_DB=0; RANGES=""; FORCE_FOLDER=0; RETIRE=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes) ASSUME_YES=1 ;;
    --retire) RETIRE+=(--retire "$2"); shift ;;
    --no-db) NO_DB=1 ;;
    --ranges) RANGES="$2"; shift ;;
    --force-folder) FORCE_FOLDER=1 ;;
    --env-file) ENV_FILE="$2"; shift ;;
    --app-dir) APP_DIR="$2"; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
  shift
done
export ASSUME_YES="${ASSUME_YES:-0}"
COMPOSE="$APP_DIR/infra/platform/docker/compose.prod.yml"

# One key's value from the env file (never a secret: only this key is read and printed).
env_value() {
  local key="$1" line val=""
  [[ -r "$ENV_FILE" ]] || { echo ""; return 0; }
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#export }"
    if [[ "${line%%=*}" == "$key" ]]; then
      val="${line#*=}"
      if [[ "$val" == \"*\" || "$val" == \'*\' ]]; then val="${val:1:${#val}-2}"; fi
    fi
  done < "$ENV_FILE"
  echo "$val"
}

# Refuse to apply (warn in a dry run, or with --force-folder).
gate() {
  if [[ "$DRY" == "1" || "$FORCE_FOLDER" == "1" ]]; then warn "$1"; else die "$1 (or pass --force-folder)"; fi
}

if [[ -z "${GOVC_USERNAME:-}" || -z "${GOVC_PASSWORD:-}" ]]; then govc_from_env_file "$ENV_FILE"; fi
step "Preflight"
lab_preflight
command -v "$PY" >/dev/null 2>&1 || die "$PY not found (set TN_LAB_PYTHON)"

# The worker must put new ranges where this script puts the old ones: the file, and what the
# running worker actually has (a file changed without a restart is not what it builds with).
folder="$(env_value VSPHERE_RANGE_FOLDER)"
if [[ -n "$folder" && "$folder" != "$TARGET_FOLDER" ]]; then
  gate "VSPHERE_RANGE_FOLDER=$folder in $ENV_FILE: new ranges would still go there. Upgrade TrueNorth (the installer now writes $TARGET_FOLDER), then restart the worker"
fi
effective="$(cd "$(dirname "$COMPOSE")" 2>/dev/null && docker compose -f "$COMPOSE" --env-file "$ENV_FILE" \
  exec -T "$WORKER_SERVICE" printenv VSPHERE_RANGE_FOLDER 2>/dev/null | tr -d '\r' || true)"
if [[ -z "$effective" ]]; then
  gate "could not read VSPHERE_RANGE_FOLDER from the running $WORKER_SERVICE container (docker compose in $APP_DIR): cannot confirm new ranges go to $TARGET_FOLDER"
elif [[ "$effective" != "$TARGET_FOLDER" ]]; then
  gate "the running $WORKER_SERVICE has VSPHERE_RANGE_FOLDER=$effective: new ranges would still go there. Restart it with $TARGET_FOLDER"
else
  info "range folder: $TARGET_FOLDER (env file and running $WORKER_SERVICE)"
fi

# The ranges, read only, in the api image. Apply runs the same command itself, fresh.
ranges_cmd="cd $(printf %q "$(dirname "$COMPOSE")") && docker compose -f $(printf %q "$COMPOSE") --env-file $(printf %q "$ENV_FILE") run --rm --no-deps -T -w /app -e PYTHONPATH=/app -v $(printf %q "$REPO:/srcapp:ro") --entrypoint python api /srcapp/scripts/lab/tn_ranges_inprocess.py 2>/dev/null"
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
plan_arg=(); apply_arg=()
if [[ -n "$RANGES" ]]; then
  plan_arg=(--ranges "$RANGES"); apply_arg=(--ranges "$RANGES")
elif [[ "$NO_DB" == "1" ]]; then
  warn "--no-db: range names unknown and nothing checks whether a range is being built or destroyed"
else
  step "TrueNorth ranges (read only, in the api image)"
  if bash -c "$ranges_cmd" > "$tmp" && [[ -s "$tmp" ]]; then
    info "$("$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); r=d["ranges"] if isinstance(d, dict) else d; print(len(r), "range(s) not destroyed")' "$tmp")"
    plan_arg=(--ranges "$tmp"); apply_arg=(--ranges-cmd "$ranges_cmd")
  elif [[ "$DRY" == "1" ]]; then
    warn "could not read the TrueNorth ranges (docker compose in $APP_DIR); planning without them"
  else
    die "could not read the TrueNorth ranges (docker compose in $APP_DIR). Without them nothing checks for a range being built; pass --no-db only if you are sure none is"
  fi
fi

step "Plan"
"$PY" "$LAB_DIR/vcenter_organize.py" plan --dc "$GOVC_DATACENTER" ${plan_arg[@]+"${plan_arg[@]}"} \
  ${RETIRE[@]+"${RETIRE[@]}"} || exit $?
if [[ "$DRY" == "1" ]]; then echo; echo "Dry run: nothing changed. Re-run without --dry-run to apply."; exit 0; fi
confirm "Apply the plan above in vCenter $(govc_url_safe)?" || die "not confirmed; nothing changed"

step "Apply (re-reads the ranges and vCenter first)"
"$PY" "$LAB_DIR/vcenter_organize.py" apply --dc "$GOVC_DATACENTER" ${apply_arg[@]+"${apply_arg[@]}"} \
  ${RETIRE[@]+"${RETIRE[@]}"}
