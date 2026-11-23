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
#   --force          remove them even while TN_TEMPLATE_ALLOW_UNPROMOTED lets ranges use
#                    unpromoted copies (below)
#   --yes            do not ask
#   --env-file FILE  vCenter credentials and TN_TEMPLATE_ALLOW_UNPROMOTED
#                    (default /srv/truenorth/config/.env.production)
#
# Why: build.sh replicate refuses to reuse a copy of the same name that is not stamped by
# the template's recorded build (ADR 0013 §2). After a same-day rebuild, the copies made
# from the template before it was recorded block replicate until removed (five times on
# 2026-11-23). A copy is removed only if it is a template (never a VM, so never one that is
# running), carries no "tn-build:" annotation line, and is not under */_retired/. A stamped
# copy - a recorded build, possibly promoted - is never touched.
#
# Only names in build.sh's copy format, <id>@<host>-<yyyymmdd>, are candidates.
# Fails closed: a copy whose template flag or annotation cannot be read is kept, with a
# warning, and the script then exits 1. In use: while TN_TEMPLATE_ALLOW_UNPROMOTED allows
# an id ("true", or a comma list naming it; the work site sets "true"), the worker clones
# the NEWEST dated copy on each host for an image with no promoted build - exactly these
# copies - so they may back live ranges. Then nothing is removed without --force (the plan
# is still printed). The value is read from the env file the worker gets (the shell only when
# that file is unreadable); the guard is open only for exactly '', 0, false, no or off (or a
# list of other ids). Unknown, unparseable, or file and shell disagreeing: treated as allowing.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
IDS=(); DATE=""; FORCE=0
ENV_FILE="${TN_ENV_FILE:-/srv/truenorth/config/.env.production}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --date) DATE="$2"; shift ;;
    --force) FORCE=1 ;;
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

# A TN_TEMPLATE_ALLOW_UNPROMOTED value as the worker would see it: inline " # comment"
# dropped, surrounding blanks and one pair of quotes trimmed, lower case.
normalise_flag() {
  local v="$1"
  v="$(printf '%s' "$v" | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//')"
  if [[ "$v" == \"*\" || "$v" == \'*\' ]]; then v="${v:1:${#v}-2}"; fi
  printf '%s' "$v" | tr '[:upper:]' '[:lower:]'
}
# The flag's value: the env file the worker reads (compose passes it through) wins; the shell
# variable only counts when that file cannot be read. "?" when neither says, "!" when both
# say different things. A readable file without the line means unset (the worker's false).
FLAG_SOURCE=""; FLAG_VALUE=""
read_flag() { # sets FLAG_VALUE and FLAG_SOURCE (no subshell, so both stick)
  local line file="" shell="" have_file=0 have_shell=0
  if [[ -r "$ENV_FILE" ]]; then
    have_file=1
    line="$(grep -E '^(export )?TN_TEMPLATE_ALLOW_UNPROMOTED=' "$ENV_FILE" | tail -n1 || true)"
    file="$(normalise_flag "${line#*=}")"
  fi
  if [[ -n "${TN_TEMPLATE_ALLOW_UNPROMOTED+set}" ]]; then
    have_shell=1; shell="$(normalise_flag "$TN_TEMPLATE_ALLOW_UNPROMOTED")"
  fi
  if [[ "$have_file" == 1 && "$have_shell" == 1 && "$file" != "$shell" ]]; then
    FLAG_SOURCE="$ENV_FILE says '$file', the shell says '$shell'"; FLAG_VALUE='!'
  elif [[ "$have_file" == 1 ]]; then FLAG_SOURCE="$ENV_FILE"; FLAG_VALUE="$file"
  elif [[ "$have_shell" == 1 ]]; then FLAG_SOURCE="the shell ($ENV_FILE unreadable)"; FLAG_VALUE="$shell"
  else FLAG_SOURCE="unknown: $ENV_FILE unreadable, not in the shell"; FLAG_VALUE='?'
  fi
}
read_flag
# 0 (allowing: refuse without --force) unless the flag is exactly off, or a well-formed list
# of ids without <id> (worker/template_pins.py). Unknown, conflicting or unparseable: allowing.
allows_unpromoted() {
  local raw="$FLAG_VALUE" p
  case "$raw" in
    ''|0|false|no|off) return 1 ;;
  esac
  [[ "$raw" =~ ^[a-z0-9][a-z0-9-]*(,[[:space:]]*[a-z0-9][a-z0-9-]*)*$ ]] || return 0
  [[ "$raw" =~ ^(1|true|yes|on)$ ]] && return 0
  IFS=',' read -r -a parts <<<"${raw//[[:space:]]/}"
  for p in "${parts[@]}"; do [[ "$p" == "$1" ]] && return 0; done
  return 1
}

step "Preflight"
lab_preflight

step "Unstamped host copies of: ${IDS[*]}${DATE:+ (dated $DATE)}"
doomed=(); unread=0; in_use=()
for id in "${IDS[@]}"; do
  vms="$(govc find / -type m -name "${id}@*${DATE:+-$DATE}" 2>/dev/null)" \
    || { warn "cannot list the copies of $id; none removed"; unread=$((unread + 1)); continue; }
  # Exactly build.sh's copy name <id>@<host>-<yyyymmdd> (that date with --date); a hand-made
  # "<id>@something" template is never a candidate.
  date_re='[0-9]{8}'; [[ -z "$DATE" ]] || date_re="$DATE"
  name_re="^${id}@[A-Za-z0-9_-]+-${date_re}\$"
  while IFS= read -r vm; do
    [[ -n "$vm" && "$vm" != */_retired/* ]] || continue
    if [[ ! "${vm##*/}" =~ $name_re ]]; then info "keep  ${vm##*/}: not a host copy name"; continue; fi
    # Every read fails closed: a copy we cannot read is kept.
    if ! tmpl="$(govc object.collect -s "$vm" config.template 2>/dev/null)"; then
      warn "keep  ${vm##*/}: cannot read whether it is a template"; unread=$((unread + 1)); continue
    fi
    if [[ "$tmpl" != "true" ]]; then info "keep  ${vm##*/}: not a template"; continue; fi
    if ! ann="$(govc object.collect -s "$vm" config.annotation 2>/dev/null)"; then
      warn "keep  ${vm##*/}: cannot read its annotation (build stamp)"; unread=$((unread + 1)); continue
    fi
    if grep -q '^tn-build:' <<<"$ann"; then info "keep  ${vm##*/}: stamped"; continue; fi
    info "unstamped ${vm}"
    doomed+=("$vm")
    allows_unpromoted "$id" && in_use+=("${vm##*/}")
  done <<<"$vms"
done
if [[ "${#doomed[@]}" -eq 0 ]]; then
  info "nothing to remove"
  [[ "$unread" -eq 0 ]] || die "$unread copy(ies) or listing(s) could not be read and were kept"
  exit 0
fi
if [[ "${#in_use[@]}" -gt 0 && "$FORCE" != "1" ]]; then
  warn "TN_TEMPLATE_ALLOW_UNPROMOTED (${FLAG_SOURCE}: '${FLAG_VALUE}') may let ranges clone the newest" \
    "unpromoted copy per host, so these may be in use: ${in_use[*]}"
  [[ "$APPLY" == "1" ]] || { info "plan only: --apply --force to remove"; exit 0; }
  die "not removed: check no range uses them, then --force (or promote a build first)"
fi
if [[ "$APPLY" == "1" ]]; then
  confirm "Remove the ${#doomed[@]} unstamped copy(ies) above (vm.destroy; they cannot be recovered)?" \
    || die "not confirmed; nothing removed"
fi
for vm in "${doomed[@]}"; do act govc vm.destroy "$vm"; done
if [[ "$APPLY" == "1" ]]; then info "removed ${#doomed[@]} unstamped copy(ies)"; else info "plan only: --apply to remove"; fi
[[ "$unread" -eq 0 ]] || die "$unread copy(ies) or listing(s) could not be read and were kept"
