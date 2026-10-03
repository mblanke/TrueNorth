# shellcheck shell=bash
# TrueNorth lab - shared helpers for scripts/lab/*.sh. Source it; do not run it.
#
# Credentials come from the environment only and are never echoed:
#   GOVC_URL       vCenter, default https://192.168.1.10/sdk (by IP: the control box
#                  cannot resolve *.truenorth.lab)
#   GOVC_USERNAME  e.g. administrator@vsphere.local
#   GOVC_PASSWORD
#   GOVC_INSECURE  default 1 (the lab VCSA has a self-signed certificate)
# Lab constants can be overridden from the environment (TN_DC, TN_CLUSTER, TN_DVS).

set -euo pipefail

: "${GOVC_URL:=https://192.168.1.10/sdk}"
: "${GOVC_INSECURE:=1}"
: "${TN_DC:=DC-Lab}"
: "${TN_CLUSTER:=CL-Lab}"
: "${TN_DVS:=vDS-10G}"
# The VCSA runs on esx01. Nothing in scripts/lab ever places or changes anything there.
: "${TN_FORBIDDEN_HOST_RE:=^(esx01([.].*)?|192[.]168[.]1[.]11)$}"
export GOVC_URL GOVC_INSECURE
export GOVC_DATACENTER="${GOVC_DATACENTER:-$TN_DC}"
# Keep govc's session cache out of ~/.govmomi on shared control boxes.
export GOVC_PERSIST_SESSION="${GOVC_PERSIST_SESSION:-false}"

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LAB_DIR

die()  { echo "ERROR: $*" >&2; exit 1; }
warn() { echo "WARN:  $*" >&2; }
info() { echo "  $*"; }
step() { echo; echo "== $*"; }

# GOVC_URL without any user:password@ part, for printing.
govc_url_safe() { printf '%s' "$GOVC_URL" | sed -E 's#(://)[^@/]*@#\1#'; }

need() { command -v "$1" >/dev/null 2>&1 || die "'$1' is not on PATH ($2)"; }

# Preflight shared by every script: tools, credentials, and that GOVC_URL is vCenter
# (a script pointed at an ESXi host would create objects vCenter does not know about,
# the same failure as `govc vm.markastemplate` against a host).
lab_preflight() {
  need govc "brew install govc, or the release binary from github.com/vmware/govmomi"
  need jq   "brew install jq / apt install jq"
  [[ -n "${GOVC_USERNAME:-}" ]] || die "set GOVC_USERNAME (e.g. administrator@vsphere.local)"
  [[ -n "${GOVC_PASSWORD:-}" ]] || die "set GOVC_PASSWORD in the environment (it is never printed)"
  export GOVC_USERNAME GOVC_PASSWORD
  local api
  api="$(govc about -json 2>/dev/null | jq -r '[.. | objects | to_entries[] | select((.key|ascii_downcase)=="apitype") | .value][0] // empty')" \
    || die "cannot reach $(govc_url_safe) with the given credentials"
  [[ "$api" == "VirtualCenter" ]] \
    || die "$(govc_url_safe) is '$api', not vCenter. Point GOVC_URL at the VCSA (https://192.168.1.10/sdk), never at an ESXi host"
  info "vCenter: $(govc_url_safe) (datacenter $GOVC_DATACENTER)"
}

is_forbidden_host() {
  local short="${1##*/}"
  [[ "$short" =~ $TN_FORBIDDEN_HOST_RE ]]
}

# jq helper: every value whose key matches $k case-insensitively, anywhere in the doc.
# govc's JSON key casing has changed between releases; this does not care.
# shellcheck disable=SC2016,SC2034 # a jq program (single quotes intended), used by the sourcing scripts
JQ_CI='def ci($k): [.. | objects | to_entries[] | select((.key|ascii_downcase)==($k|ascii_downcase)) | .value];'

# Run a changing command, or print it in plan mode. APPLY=1 to run.
APPLY="${APPLY:-0}"
act() {
  if [[ "$APPLY" == "1" ]]; then
    echo "  + $*"
    "$@"
  else
    echo "  [plan] $*"
  fi
}

# pyVmomi helper for the two things govc cannot set (port group security policy,
# removing a clone's vApp/OVF config). Same GOVC_* environment.
vim_helper() {
  local py="${TN_LAB_PYTHON:-python3}"
  "$py" -c 'import pyVmomi' 2>/dev/null \
    || die "$py has no pyVmomi. pip install 'pyvmomi==8.0.3.0.1' (or set TN_LAB_PYTHON to a python that has it)"
  "$py" "$LAB_DIR/vim_helper.py" "$@"
}
