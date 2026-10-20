#!/usr/bin/env bash
# TrueNorth work site - ADR 0012 decision 9: does a clone of template <id> take TrueNorth's
# customization on every range host? Clone, customize, power on, wait for VMware Tools to
# report the new name and IP, then destroy the clone.
#
#   scripts/lab/test-clone.sh srv2022                     # asks y/N, then esx02 esx03 esx04
#   scripts/lab/test-clone.sh ubuntu-lts --hosts esx04 --yes
#   scripts/lab/test-clone.sh pfsense --mode power        # appliances without Tools
#
# Options:
#   --hosts "H..."   range hosts (default "esx02 esx03 esx04", or RANGE_HOSTS)
#   --mode M         customize (default): guest customization (Windows Sysprep name + IP +
#                    a throwaway Administrator password; Linux name + IP), pass when Tools
#                    reports that name and IP
#                    tools: no customization, pass when Tools is running
#                    power: no customization, pass when the VM is still powered on after 90 s
#   --network PG     port group for the clone's first NIC (default dPG-TN-BUILD)
#   --ip-base A.B.C  static test addresses A.B.C.241.. (default 10.30.31; no gateway)
#   --timeout MIN    per clone (default 25: Sysprep takes a while)
#   --keep           leave the clones (default: destroy each one that passed)
#   --yes            do not ask
#   --env-file FILE  vCenter credentials (default /srv/truenorth/config/.env.production)
#
# Each clone is tagged tn-type=test-clone, tn-template=<id> (best effort; TN_TAGS=0: off).
#
# Source per host: the newest host-local copy <id>@<host>-<yyyymmdd> (build.sh replicate),
# else <id>; the clone goes on that host's own datastore (<host>-local), as the
# provisioner does. A failed clone is kept for diagnosis; its destroy command is printed.
# Exit 0 only if every host passed.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
ID=""; HOSTS="${RANGE_HOSTS:-esx02 esx03 esx04}"; MODE="customize"; NET="dPG-TN-BUILD"
IP_BASE="10.30.31"; TIMEOUT_MIN=25; KEEP=0
ENV_FILE="${TN_ENV_FILE:-/srv/truenorth/config/.env.production}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hosts) HOSTS="$2"; shift ;;
    --mode) MODE="$2"; shift ;;
    --network) NET="$2"; shift ;;
    --ip-base) IP_BASE="$2"; shift ;;
    --timeout) TIMEOUT_MIN="$2"; shift ;;
    --keep) KEEP=1 ;;
    --yes) ASSUME_YES=1 ;;
    --env-file) ENV_FILE="$2"; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown option: $1" >&2; usage 2 ;;
    *) [[ -z "$ID" ]] || usage 2; ID="$1" ;;
  esac
  shift
done
[[ -n "$ID" ]] || usage 2
case "$MODE" in customize|tools|power) ;; *) die "--mode customize|tools|power" ;; esac
[[ "$TIMEOUT_MIN" =~ ^[0-9]+$ ]] || die "--timeout takes minutes"
if [[ -z "${GOVC_USERNAME:-}" || -z "${GOVC_PASSWORD:-}" ]]; then govc_from_env_file "$ENV_FILE"; fi
APPLY=1   # every change below is confirmed first; act() echoes each command

collect() { govc object.collect -s "$1" "$2" 2>/dev/null || true; }

find_host() {
  local p
  p="$(govc find / -type h -name "$1" | head -n1)"
  [[ -n "$p" ]] || p="$(govc find / -type h -name "$1.*" | head -n1)"
  echo "$p"
}

# Newest <id>@<host>-<yyyymmdd> on that host, else the plain template.
source_for() {
  local short="$1" copy
  copy="$(govc find / -type m -name "${ID}@${short}-*" | sort | tail -n1)"
  if [[ -n "$copy" ]]; then echo "$copy"; else govc find / -type m -name "$ID" | head -n1; fi
}

# Network, customization (customize mode) and power-on. Non-zero: the clone is not testable.
prepare() {
  local vm="$1" nic="$2" want_name="$3" ip="$4"
  if [[ -n "$nic" ]]; then act govc vm.network.change -vm "$vm" -net "$NET" "$nic" || return 1; fi
  if [[ "$MODE" == "customize" ]]; then
    case "$(collect "$vm" config.guestId)" in
      win*) # A throwaway password, set by Sysprep like a range password; never printed.
            echo "  + govc vm.customize -vm $vm -name $want_name -ip $ip -netmask 255.255.255.0 -pwd <random>"
            govc vm.customize -vm "$vm" -name "$want_name" -ip "$ip" -netmask 255.255.255.0 \
              -pwd "Tc!$(od -An -N9 -tx1 /dev/urandom | tr -d ' \n')" || return 1 ;;
      *)    act govc vm.customize -vm "$vm" -name "$want_name" -ip "$ip" -netmask 255.255.255.0 || return 1 ;;
    esac
  fi
  act govc vm.power -on "$vm"
}

step "Preflight"
lab_preflight
CLUSTER_PATH="$(govc find / -type c -name "$TN_CLUSTER" | head -n1)"
[[ -n "$CLUSTER_PATH" ]] || die "cluster $TN_CLUSTER not found"
[[ -n "$(govc find / -type m -name "$ID" | head -n1)" ]] || die "template '$ID' is not in vCenter inventory; build it first"
[[ -n "$(govc find / -type n -name "$NET" | head -n1)" ]] || die "port group $NET not found (--network)"
read -r -a hosts <<<"$HOSTS"

step "Plan ($MODE)"
idx=0; plan=()
for h in "${hosts[@]}"; do
  short="${h%%.*}"; idx=$((idx + 1))
  is_forbidden_host "$short" && die "refusing $short: esx01 holds the VCSA and takes no ranges"
  host_path="$(find_host "$short")"; [[ -n "$host_path" ]] || die "host $short not found"
  src="$(source_for "$short")"
  name="tn-testclone-${ID}-${short}"
  ip="${IP_BASE}.$((240 + idx))"
  info "$short: clone ${src##*/} -> $name on ${short}-local, $NET$([[ "$MODE" == customize ]] && echo ", name tntc-${short}, ip $ip")"
  plan+=("$short|$host_path|$src|$name|$ip")
done
confirm "Clone, power on, check and destroy ${#plan[@]} test clone(s) of $ID?" || die "not confirmed; nothing changed"

failed=0
for item in "${plan[@]}"; do
  IFS='|' read -r short host_path src name ip <<<"$item"
  step "$short: $name"
  vm="$(govc find / -type m -name "$name" | head -n1)"
  if [[ -n "$vm" ]]; then
    info "a previous $name exists; destroying it first"
    act govc vm.destroy "$vm"
  fi
  folder="${TN_TEST_FOLDER:-$(dirname "$src")}"
  if ! act govc vm.clone -vm "$src" -on=false -host "$host_path" -ds "${short}-local" \
      -pool "$CLUSTER_PATH/Resources" -folder "$folder" "$name"; then
    warn "$short: clone failed"; failed=1; continue
  fi
  vm="$(govc find / -type m -name "$name" | head -n1)"
  if [[ "${TN_TAGS:-1}" != "0" ]]; then  # tn-type=test-clone: one left for diagnosis is easy to find
    "${TN_LAB_PYTHON:-python3}" "$LAB_DIR/vcenter_organize.py" tag "$vm" --type test-clone --template "$ID" \
      || warn "$short: test clone not tagged (vCenter tagging failed); the test goes on"
  fi
  nic="$(govc device.ls -vm "$vm" | awk '/^ethernet-/{print $1; exit}')"
  want_name="tntc-${short}"
  if ! prepare "$vm" "$nic" "$want_name" "$ip"; then
    warn "FAIL $short: could not prepare the clone. Kept for diagnosis; remove with: govc vm.destroy '$vm'"
    failed=1; continue
  fi

  deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 )); ok=0; started=$(date +%s)
  while (( $(date +%s) < deadline )); do
    sleep "${TN_TEST_POLL:-15}"
    got_ip="$(collect "$vm" guest.ipAddress)"; got_name="$(collect "$vm" guest.hostName)"
    tools="$(collect "$vm" guest.toolsRunningStatus)"; power="$(collect "$vm" runtime.powerState)"
    case "$MODE" in
      customize)
        shopt -s nocasematch
        if [[ "$got_ip" == "$ip" && "${got_name%%.*}" == "$want_name" ]]; then ok=1; fi
        shopt -u nocasematch ;;
      tools) [[ "$tools" == "guestToolsRunning" ]] && ok=1 ;;
      power) [[ "$power" == "poweredOn" && $(( $(date +%s) - started )) -ge 90 ]] && ok=1 ;;
    esac
    (( ok )) && break
    [[ "$power" == "poweredOn" ]] || break
  done
  info "tools=${tools:-?} power=${power:-?} ip=${got_ip:-none} name=${got_name:-none}"
  if (( ok )); then
    info "PASS $short"
    if [[ "$KEEP" == "1" ]]; then info "kept (--keep): govc vm.destroy '$vm'"
    else act govc vm.destroy "$vm"; fi
  else
    warn "FAIL $short: $MODE check did not pass in ${TIMEOUT_MIN} min. Kept for diagnosis; remove with: govc vm.destroy '$vm'"
    failed=1
  fi
done
if [[ "$failed" == "0" ]]; then echo "RESULT: $ID test clones PASSED on: $HOSTS"; else echo "RESULT: $ID test clones FAILED (see above)"; fi
exit "$failed"
