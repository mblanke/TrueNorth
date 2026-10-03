#!/usr/bin/env bash
# TrueNorth lab - create the build and depot port groups on vDS-10G (runbook §4.1a).
#
#   scripts/lab/create-portgroups.sh            # plan only (default): read-only
#   scripts/lab/create-portgroups.sh --apply    # create what is missing
#
#   dPG-TN-BUILD  VLAN 31  10.30.31.0/24  TN-BUILD01 (internet egress) + TN-DEPOT01's uplink NIC
#   dPG-TN-SVC    VLAN 32  10.30.32.0/24  TN-DEPOT01 + range pfSense WANs (no egress)
#
# Idempotent. Before changing anything it reads every distributed and standard port
# group in the datacenter and refuses if VLAN 31 or 32 is already used by a port group
# with another name (uplink port groups, which trunk everything, are ignored). Each new
# port group gets an explicit security policy: promiscuous, MAC changes and forged
# transmits all reject. govc cannot set that, so it goes through vim_helper.py (pyVmomi)
# when the inherited policy is not already reject/reject/reject.
#
# It never touches UniFi. It prints the UniFi checklist at the end; a human applies it.
# Credentials: GOVC_USERNAME / GOVC_PASSWORD from the environment (see lib.sh).
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dvs) TN_DVS="$2"; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
  shift
done

WANT=("dPG-TN-BUILD:31" "dPG-TN-SVC:32")

step "Preflight"
lab_preflight
dvs_paths="$(govc find / -type w -name "$TN_DVS")"
[[ -n "$dvs_paths" && "$(wc -l <<<"$dvs_paths" | tr -d ' ')" == "1" ]] \
  || die "expected one distributed switch named $TN_DVS, found: ${dvs_paths:-none}"
DVS_PATH="$dvs_paths"
DVS_MOID="$(govc ls -i "$DVS_PATH" | sed 's/.*://')"
info "switch: $DVS_PATH ($DVS_MOID)"

step "VLANs in use (distributed and standard port groups)"
# lines: <vlan|lo-hi> <kind> <name>
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
inuse="$work/inuse"; existing="$work/existing"   # existing: <name> <vlans> <dvs-moid>
: >"$inuse"; : >"$existing"
while IFS= read -r pg; do
  [[ -n "$pg" ]] || continue
  name="${pg##*/}"
  cfg="$(govc object.collect -json "$pg" config)"
  uplink="$(jq -r "$JQ_CI"' ci("uplink")[0] // false' <<<"$cfg")"
  [[ "$uplink" == "true" ]] && continue
  dvs="$(jq -r "$JQ_CI"' ci("distributedVirtualSwitch")[0] | (.value // .Value // "")' <<<"$cfg")"
  # vlanId is an int (VLAN), an array of {start,end} (trunk), or absent (PVLAN).
  vlans="$(jq -r "$JQ_CI"' ci("vlan")[0] | ci("vlanId")[0] |
      if type=="number" then tostring
      elif type=="array" then (map("\(.start // .Start)-\(.end // .End)") | join(","))
      else "?" end' <<<"$cfg")"
  echo "$name $vlans ${dvs:--}" >>"$existing"
  echo "$vlans dvpg $name" >>"$inuse"
done < <(govc find / -type g)
while IFS= read -r host; do
  [[ -n "$host" ]] || continue
  govc host.portgroup.info -json -host "$host" \
    | jq -r '[.. | objects | with_entries(.key |= ascii_downcase) | select(has("vlanid") and has("name"))]
             | unique_by(.name)[] | "\(.vlanid) std:'"${host##*/}"' \(.name)"' >>"$inuse"
done < <(govc find / -type h)
sort -n "$inuse" | sed 's/^/  /'

vlan_used_by_other() { # $1 vlan, $2 our name -> prints conflicting port groups
  local v="$1" self="$2" spec kind name a b r
  while read -r spec kind name; do
    [[ "$kind" == "dvpg" && "$name" == "$self" ]] && continue
    IFS=',' read -ra ranges <<<"$spec"
    for r in "${ranges[@]}"; do
      if [[ "$r" == *-* ]]; then a="${r%-*}"; b="${r#*-}"; else a="$r"; b="$r"; fi
      [[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ ]] || continue
      if (( v >= a && v <= b )); then echo "$kind $name (VLAN $spec)"; fi
    done
  done <"$inuse"
}

existing_field() { awk -v n="$1" -v f="$2" '$1==n {print $f; exit}' "$existing"; }

step "Plan"
conflicts=0
todo=()
for w in "${WANT[@]}"; do
  name="${w%%:*}"; vlan="${w##*:}"
  (( vlan < 100 || vlan > 199 )) || die "VLAN $vlan is in the range block 100-199 (TrueNorth ranges only)"
  other="$(vlan_used_by_other "$vlan" "$name")"
  if [[ -n "$other" ]]; then
    echo "  REFUSE $name: VLAN $vlan is already used by:"
    while IFS= read -r l; do echo "           $l"; done <<<"$other"
    conflicts=1; continue
  fi
  ex_vlan="$(existing_field "$name" 2)"; ex_dvs="$(existing_field "$name" 3)"
  if [[ -n "$ex_vlan" ]]; then
    if [[ "$ex_vlan" != "$vlan" ]]; then
      echo "  REFUSE $name exists with VLAN $ex_vlan, expected $vlan. Fix it by hand."; conflicts=1; continue
    fi
    if [[ "$ex_dvs" != "-" && "$ex_dvs" != "$DVS_MOID" ]]; then
      echo "  REFUSE $name exists on another switch ($ex_dvs)."; conflicts=1; continue
    fi
    info "ok     $name (VLAN $vlan) exists"
  else
    info "create $name (VLAN $vlan) on $TN_DVS"
  fi
  todo+=("$name:$vlan")
done
(( conflicts == 0 )) || die "VLAN conflict: nothing was changed. Pick free VLANs, update runbook §4.1a, and re-run"

step "Apply"
for w in ${todo[@]+"${todo[@]}"}; do
  name="${w%%:*}"; vlan="${w##*:}"
  if [[ -z "$(existing_field "$name" 2)" ]]; then
    act govc dvs.portgroup.add -dvs "$DVS_PATH" -type earlyBinding -nports 32 -auto-expand=true -vlan "$vlan" "$name"
  fi
  if [[ "$APPLY" == "1" ]]; then
    pg_path="$(govc find / -type g -name "$name" | head -n1)"
    [[ -n "$pg_path" ]] || die "$name not found after create"
    sec="$(govc object.collect -json "$pg_path" config.defaultPortConfig \
           | jq -c "$JQ_CI"' ci("securityPolicy")[0] | [ci("allowPromiscuous")[0], ci("macChanges")[0], ci("forgedTransmits")[0]] | map(if type=="object" then (if has("value") then .value else .Value end) else . end)')"
    if [[ "$sec" == "[false,false,false]" ]]; then
      info "ok     $name security policy reject/reject/reject (inherited or explicit)"
    else
      info "fix    $name security policy is $sec"
      vim_helper pg-security --name "$name"
    fi
  else
    info "[plan] ensure $name security policy: promiscuous=reject, MAC changes=reject, forged transmits=reject"
  fi
done
[[ "$APPLY" == "1" ]] || { echo; echo "Plan only. Re-run with --apply to change vCenter."; }

cat <<'EOF'

== UniFi checklist (a human does this; this script never touches UniFi)
  Networks (VLAN-only on the 10G trunk ports to esx01-04; they must also be tagged on
  the host ports, like 30 and 40):
    [ ] TN-BUILD  VLAN 31  10.30.31.0/24  gateway 10.30.31.1  DHCP .100-.200
    [ ] TN-SVC    VLAN 32  10.30.32.0/24  gateway 10.30.32.1  DHCP OFF
  Firewall rules (LAN IN, in this order):
    [ ] allow  10.30.31.0/24 -> internet (TN-BUILD01 and, during a prefetch window,
               TN-DEPOT01's uplink NIC 10.30.31.11)
    [ ] allow  10.30.31.0/24 -> 192.168.1.10 tcp/443 and 192.168.1.11-14 tcp/443,902
               (Packer uploads, ISO mounts)
    [ ] allow  10.30.31.10 -> 10.30.32.10 tcp/22,8081,3142 (Ansible, Nexus API, prefetch)
    [ ] allow  <control box> -> 10.30.31.10 tcp/22 (Ansible reaches the depot through it)
    [ ] drop   10.30.31.0/24 -> 10.30.30.0/24 (no path to TN-MGMT01)
    [ ] allow  10.30.32.0/24 -> 10.30.32.10 tcp/8081,3142 (range pfSense WANs; same L2,
               listed for completeness - this traffic does not cross the gateway)
    [ ] drop   10.30.32.0/24 -> internet, 10.30.30.0/24, 192.168.1.0/24, 10.30.31.0/24
               (new connections; replies to the allowed BUILD01 rule must still pass)
  Check:
    [ ] no network/SVI exists for VLANs 100-199 (runbook §3)
EOF
