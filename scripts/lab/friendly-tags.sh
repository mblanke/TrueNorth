#!/usr/bin/env bash
# Author: Marc-Andre Blanke
# Created: 2026-11-23T20:47:00Z
# TrueNorth work site - human-readable vCenter tags on the golden templates and their host
# copies, next to the tn-* tags build.sh and vcenter-organize.sh set:
#   category "OS"    Windows | Linux | Network appliance
#   category "Role"  Server | Workstation | Security tool | Security application |
#                    Network appliance | Simulation
#
#   scripts/lab/friendly-tags.sh                 # plan: what would be created and attached
#   scripts/lab/friendly-tags.sh --apply         # create and attach (asks y/N first)
#
# Options:
#   --folder PATH    where the templates are (default /<datacenter>/vm/TrueNorth/Templates;
#                    searched recursively, so the "Host copies" subfolders are included)
#   --apply          make the changes (default: print the plan only)
#   --yes            do not ask
#   --env-file FILE  vCenter credentials (default /srv/truenorth/config/.env.production)
#
# Idempotent: categories and tags are created once, only missing attachments are made;
# safe to re-run. Test clones (tn-testclone-*), */_retired/ and ids it has no mapping for
# are skipped (the last are listed).
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
FOLDER=""
ENV_FILE="${TN_ENV_FILE:-/srv/truenorth/config/.env.production}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --folder) FOLDER="$2"; shift ;;
    --apply) APPLY=1 ;;
    --yes) ASSUME_YES=1 ;;
    --env-file) ENV_FILE="$2"; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
  shift
done
FOLDER="${FOLDER:-/$GOVC_DATACENTER/vm/TrueNorth/Templates}"
if [[ -z "${GOVC_USERNAME:-}" || -z "${GOVC_PASSWORD:-}" ]]; then govc_from_env_file "$ENV_FILE"; fi

# "<OS>|<Role>" of a catalogue id or variant, or nothing (bash 3.2: no associative arrays).
os_role_of() {
  case "$1" in
    ubuntu-lts|rocky|debian13|ca-host|ubuntu-webstack) echo "Linux|Server" ;;
    kali|kali-gui|parrot|remnux|sift) echo "Linux|Security tool" ;;
    securityonion) echo "Linux|Security application" ;;
    svc-emulators|usersim|cloudlog-emu|greyspace-host) echo "Linux|Simulation" ;;
    srv2016|srv2019|srv2022|srv2025|srv2022-*) echo "Windows|Server" ;;
    win7-sp1|win10-22h2|win10-ltsc|win11-24h2|win11-analyst|win11-dev|win10-office) echo "Windows|Workstation" ;;
    precomp-host|detonation-host) echo "Windows|Simulation" ;;
    vyos|pfsense) echo "Network appliance|Network appliance" ;;
  esac
}

step "Preflight"
lab_preflight
govc folder.info "$FOLDER" >/dev/null 2>&1 || die "folder $FOLDER not found (--folder)"
if [[ "$APPLY" == "1" ]]; then
  confirm "Create the OS/Role tag categories and tags where missing, and attach them under $FOLDER?" \
    || die "not confirmed; nothing changed"
fi

made=" "
ensure_category() {
  [[ "$made" == *" cat:$1 "* ]] && return 0
  made+="cat:$1 "
  govc tags.category.info "$1" >/dev/null 2>&1 || act govc tags.category.create -t VirtualMachine -d "$2" "$1"
}
ensure_tag() {
  [[ "$made" == *" tag:$1/$2 "* ]] && return 0
  made+="tag:$1/$2 "
  govc tags.info -c "$1" "$2" >/dev/null 2>&1 || act govc tags.create -c "$1" "$2"
}

step "Tags on the templates under $FOLDER"
ensure_category "OS" "TrueNorth: operating system family of the image"
ensure_category "Role" "TrueNorth: what the image is for"
n=0; unmapped=()
while IFS= read -r vm; do
  [[ -n "$vm" ]] || continue
  name="${vm##*/}"; base="${name%%@*}"
  [[ "$name" == tn-testclone-* || "$vm" == */_retired/* ]] && continue
  pair="$(os_role_of "$base")"
  [[ -n "$pair" ]] || { unmapped+=("$name"); continue; }
  have="$(govc tags.attached.ls -r "$vm" 2>/dev/null || true)"
  for cat_tag in "OS|${pair%%|*}" "Role|${pair#*|}"; do
    cat="${cat_tag%%|*}"; tag="${cat_tag#*|}"
    grep -qxF "$tag" <<<"$have" && continue
    ensure_tag "$cat" "$tag"
    act govc tags.attach -c "$cat" "$tag" "$vm"
    n=$((n + 1))
  done
done < <(govc find "$FOLDER" -type m 2>/dev/null || true)
[[ "${#unmapped[@]}" -eq 0 ]] || warn "no OS/Role mapping (add it to os_role_of): ${unmapped[*]}"
info "$n tag attachment(s) $([[ "$APPLY" == "1" ]] && echo made || echo "planned; --apply to make them")"
