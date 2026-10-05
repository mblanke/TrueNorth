#!/usr/bin/env bash
# TrueNorth lab - connect or disconnect TN-DEPOT01's uplink NIC (its only internet path).
#
#   scripts/lab/depot-uplink.sh status
#   scripts/lab/depot-uplink.sh on  [--apply] [--force]
#   scripts/lab/depot-uplink.sh off [--apply]
#
# Why this exists. The depot's caches (Nexus proxy repos, apt-cacher-ng) fetch from
# upstream themselves: whoever asks, the DEPOT makes the outbound request. dPG-TN-SVC
# has no internet by design, so the depot has a second NIC on dPG-TN-BUILD
# (10.30.31.11) that is connected only for an install or prefetch window. Outside the
# window the NIC is disconnected at the vCenter level, so no software setting on the
# depot can give a range a path out. install/lab/prefetch.yml also switches the caches
# to offline mode (apt-cacher-ng Offlinemode, Nexus proxies blocked) at the end of a
# window; the two controls are independent on purpose.
#
# `on` refuses while range VMs exist (folder truenorth/ranges) unless --force: during a
# window a range could pull packages from upstream through the depot.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
CMD=""; FORCE=0; VM_NAME="TN-DEPOT01"; UPLINK_PG="dPG-TN-BUILD"
: "${TN_RANGE_FOLDER:=truenorth/ranges}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    on|off|status) CMD="$1" ;;
    --apply) APPLY=1 ;;
    --force) FORCE=1 ;;
    --vm) VM_NAME="$2"; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
  shift
done
[[ -n "$CMD" ]] || usage 2

lab_preflight
VM="$(govc find / -type m -name "$VM_NAME")"
[[ -n "$VM" ]] || die "$VM_NAME not found"
pg_path="$(govc find / -type g -name "$UPLINK_PG")"
[[ -n "$pg_path" ]] || die "port group $UPLINK_PG not found"
pg_key="$(govc object.collect -s "$pg_path" key)"

# The uplink is the NIC whose backing is dPG-TN-BUILD. Never guess by position.
nic=""
for dev in $(govc device.ls -vm "$VM" | awk '/^ethernet-/{print $1}'); do
  key="$(govc device.info -json -vm "$VM" "$dev" | jq -r "$JQ_CI"' ci("portgroupKey")[0] // empty')"
  if [[ "$key" == "$pg_key" ]]; then nic="$dev"; fi
done
[[ -n "$nic" ]] || die "$VM_NAME has no NIC on $UPLINK_PG (deployed with --no-uplink?)"

state() {
  govc device.info -json -vm "$VM" "$nic" \
    | jq -r "$JQ_CI"' ci("connectable")[0] | "connected=\(ci("connected")[0]) startConnected=\(ci("startConnected")[0])"'
}
info "$VM_NAME $nic on $UPLINK_PG: $(state)"

case "$CMD" in
  status) exit 0 ;;
  on)
    ranges="$(govc find "/$GOVC_DATACENTER/vm/$TN_RANGE_FOLDER" -type m 2>/dev/null | wc -l | tr -d ' ')"
    if [[ "$ranges" != "0" ]]; then
      if [[ "$FORCE" == "1" ]]; then
        warn "$ranges range VM(s) exist; connecting anyway (--force). Their pfSense WANs can reach the depot's caches while it is online"
      else
        die "$ranges range VM(s) exist under $TN_RANGE_FOLDER. Destroy them, or pass --force to accept that they can pull from upstream through the depot during the window"
      fi
    fi
    act govc device.connect -vm "$VM" "$nic"
    [[ "$APPLY" == "1" ]] && info "now: $(state). Close the window with: $0 off --apply"
    ;;
  off)
    act govc device.disconnect -vm "$VM" "$nic"
    [[ "$APPLY" == "1" ]] && info "now: $(state)"
    ;;
esac
[[ "$APPLY" == "1" ]] || echo "Plan only. Re-run with --apply."
