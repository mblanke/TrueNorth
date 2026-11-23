#!/usr/bin/env bash
# Author: Marc-Andre Blanke
# Created: 2026-11-23T20:47:00Z
# TrueNorth work site - remove host copies <id>@<host>-<yyyymmdd> that carry NO tn-build stamp.
#
#   scripts/lab/clear-unstamped-copies.sh srv2022 kali              # plan: what would go
#   scripts/lab/clear-unstamped-copies.sh srv2022 --date 20261123 --apply   # asks y/N first
#
# Options:
#   --date YYYYMMDD  only copies of that date (build-phase.sh passes today's REPLICA_DATE)
#   --apply          remove them (default: print the plan only)
#   --yes            do not ask
#   --env-file FILE  vCenter credentials (default /srv/truenorth/config/.env.production)
#
# Why: build.sh replicate refuses to reuse a copy of the same name that is not stamped by
# the template's recorded build (ADR 0013 §2). After a same-day rebuild, the copies made
# from the template before it was recorded block replicate until removed (five times on
# 2026-11-23). A copy is removed only if it is a template (never a VM, so never one that is
# running), carries no "tn-build:" annotation line, and is not under */_retired/. A stamped
# copy - a recorded build, possibly promoted - is never touched.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
IDS=(); DATE=""
ENV_FILE="${TN_ENV_FILE:-/srv/truenorth/config/.env.production}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --date) DATE="$2"; shift ;;
    --apply) APPLY=1 ;;
    --yes) ASSUME_YES=1 ;;
    --env-file) ENV_FILE="$2"; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown option: $1" >&2; usage 2 ;;
    *) IDS+=("$1") ;;
  esac
  shift
done
[[ "${#IDS[@]}" -gt 0 ]] || usage 2
[[ -z "$DATE" || "$DATE" =~ ^[0-9]{8}$ ]] || die "--date takes YYYYMMDD"
for id in "${IDS[@]}"; do [[ "$id" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "not a template id: $id"; done
if [[ -z "${GOVC_USERNAME:-}" || -z "${GOVC_PASSWORD:-}" ]]; then govc_from_env_file "$ENV_FILE"; fi

collect() { govc object.collect -s "$1" "$2" 2>/dev/null || true; }

step "Preflight"
lab_preflight

step "Unstamped host copies of: ${IDS[*]}${DATE:+ (dated $DATE)}"
doomed=()
for id in "${IDS[@]}"; do
  while IFS= read -r vm; do
    [[ -n "$vm" && "$vm" != */_retired/* ]] || continue
    if [[ "$(collect "$vm" config.template)" != "true" ]]; then info "keep  ${vm##*/}: not a template"; continue; fi
    if collect "$vm" config.annotation | grep -q '^tn-build:'; then info "keep  ${vm##*/}: stamped"; continue; fi
    info "unstamped ${vm}"
    doomed+=("$vm")
  done < <(govc find / -type m -name "${id}@*${DATE:+-$DATE}" 2>/dev/null || true)
done
[[ "${#doomed[@]}" -gt 0 ]] || { info "nothing to remove"; exit 0; }
if [[ "$APPLY" == "1" ]]; then
  confirm "Remove the ${#doomed[@]} unstamped copy(ies) above (vm.destroy; they cannot be recovered)?" \
    || die "not confirmed; nothing removed"
fi
for vm in "${doomed[@]}"; do act govc vm.destroy "$vm"; done
if [[ "$APPLY" == "1" ]]; then info "removed ${#doomed[@]} unstamped copy(ies)"; else info "plan only: --apply to remove"; fi
