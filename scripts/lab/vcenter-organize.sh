#!/usr/bin/env bash
# TrueNorth work site - tidy TrueNorth's objects in vCenter: one TrueNorth folder tree and
# tn-* tags (ADR 0012 follow-up). Idempotent; run it on TN-MGMT01 once the templates are built.
#
#   sudo scripts/lab/vcenter-organize.sh --dry-run     # read-only: print the plan
#   sudo scripts/lab/vcenter-organize.sh               # print the plan, ask y/N, apply it
#
# What it does (details: scripts/lab/vcenter_organize.py):
#   - folders TrueNorth/Templates, TrueNorth/Templates/_retired, TrueNorth/Ranges
#   - templates from truenorth/templates -> TrueNorth/Templates (catalogue ids and their
#     host copies) or TrueNorth/Templates/_retired (anything else, e.g. kali-linux; NOT deleted)
#   - range folders truenorth/ranges/<id8> -> TrueNorth/Ranges/<range-name>-<id8> (names from
#     the TrueNorth database; id8 only with --no-db)
#   - the old truenorth folders, each only once vCenter reports it empty
#   - tags tn-type / tn-os / tn-template / tn-range on golden templates, host copies, test
#     clones, range VMs, TN-DEPOT01 and TN-MGMT01. Nothing else in vCenter is touched.
# Moving a VM between folders does not touch it (running VMs keep running). It refuses while
# a TrueNorth range is provisioning or destroying.
#
# Options:
#   --dry-run        print the plan; change nothing
#   --yes            do not ask
#   --no-db          do not read the TrueNorth ranges (folders keep id8 names, and nothing
#                    checks for a range being built: be sure none is)
#   --ranges FILE    the ranges as JSON instead (scripts/lab/tn_ranges_inprocess.py output)
#   --force-folder   apply although VSPHERE_RANGE_FOLDER in the env file is not TrueNorth/Ranges
#   --env-file FILE  vCenter credentials (default /srv/truenorth/config/.env.production)
#   --app-dir DIR    installed app (compose file), default /srv/truenorth/app
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
REPO="$(cd "$LAB_DIR/../.." && pwd)"
ENV_FILE="${TN_ENV_FILE:-/srv/truenorth/config/.env.production}"
APP_DIR="${TN_APP_DIR:-/srv/truenorth/app}"
PY="${TN_LAB_PYTHON:-python3}"
TARGET_FOLDER="TrueNorth/Ranges"
DRY=0; NO_DB=0; RANGES=""; FORCE_FOLDER=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes) ASSUME_YES=1 ;;
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

if [[ -z "${GOVC_USERNAME:-}" || -z "${GOVC_PASSWORD:-}" ]]; then govc_from_env_file "$ENV_FILE"; fi
step "Preflight"
lab_preflight
command -v "$PY" >/dev/null 2>&1 || die "$PY not found (set TN_LAB_PYTHON)"

# The worker must put new ranges where this script puts the old ones.
folder="$(env_value VSPHERE_RANGE_FOLDER)"
if [[ -n "$folder" && "$folder" != "$TARGET_FOLDER" ]]; then
  msg="VSPHERE_RANGE_FOLDER=$folder in $ENV_FILE: new ranges would still go there. Upgrade TrueNorth (the installer now writes $TARGET_FOLDER) or set it and restart the worker"
  if [[ "$DRY" == "1" || "$FORCE_FOLDER" == "1" ]]; then warn "$msg"; else die "$msg (or pass --force-folder)"; fi
else
  info "range folder: ${folder:-<installer default>} -> $TARGET_FOLDER"
fi

tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
ranges_arg=()
if [[ -n "$RANGES" ]]; then
  cp "$RANGES" "$tmp"; ranges_arg=(--ranges "$tmp")
elif [[ "$NO_DB" == "1" ]]; then
  warn "--no-db: range names unknown and nothing checks whether a range is being built or destroyed"
else
  step "TrueNorth ranges (read only, in the api image)"
  compose="$APP_DIR/infra/platform/docker/compose.prod.yml"
  if (cd "$(dirname "$compose")" && docker compose -f "$compose" --env-file "$ENV_FILE" run --rm --no-deps -T -w /app \
        -e PYTHONPATH=/app -v "$REPO:/srcapp:ro" --entrypoint python api /srcapp/scripts/lab/tn_ranges_inprocess.py) \
        > "$tmp" 2>/dev/null && [[ -s "$tmp" ]]; then
    info "$("$PY" -c 'import json,sys; r=json.load(open(sys.argv[1])); print(len(r), "range(s) not destroyed")' "$tmp")"
    ranges_arg=(--ranges "$tmp")
  elif [[ "$DRY" == "1" ]]; then
    warn "could not read the TrueNorth ranges (docker compose in $APP_DIR); planning without them"
  else
    die "could not read the TrueNorth ranges (docker compose in $APP_DIR). Without them nothing checks for a range being built; pass --no-db only if you are sure none is"
  fi
fi

step "Plan"
"$PY" "$LAB_DIR/vcenter_organize.py" plan --dc "$GOVC_DATACENTER" ${ranges_arg[@]+"${ranges_arg[@]}"} || exit $?
if [[ "$DRY" == "1" ]]; then echo; echo "Dry run: nothing changed. Re-run without --dry-run to apply."; exit 0; fi
confirm "Apply the plan above in vCenter $(govc_url_safe)?" || die "not confirmed; nothing changed"

step "Apply"
"$PY" "$LAB_DIR/vcenter_organize.py" apply --dc "$GOVC_DATACENTER" ${ranges_arg[@]+"${ranges_arg[@]}"}
