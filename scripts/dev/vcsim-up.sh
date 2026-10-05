#!/usr/bin/env bash
# Launch govmomi's vCenter simulator (vcsim) shaped like the TrueNorth lab, and seed it
# with what the vsphere_api provisioner expects: a vDS (vDS-10G) on every host, the
# management port group (dPG-TN-MGMT, VLAN 30), the depot uplink (dPG-TN-SVC, VLAN 32),
# and the inventory VM templates tmpl-ubuntu-2404 / tmpl-pfsense / tmpl-win2022 with
# their one NIC on the management port group (as on site, so a NIC the provisioner
# forgets to re-point shows up as "on the management network").
#
#   scripts/dev/vcsim-up.sh [PORT]          launch on 127.0.0.1:PORT (default 8989), seed,
#                                           print the env to point the provisioner at it
#   scripts/dev/vcsim-up.sh --seed-only     seed the simulator GOVC_URL points at
#                                           (tests/integration/test_vsphere_vcsim.py)
#
# Needs: vcsim and govc. govc: `brew install govc`. vcsim is not in Homebrew and
# `go install .../vcsim@latest` refuses (its go.mod has replace directives); build it:
#   git clone --depth 1 --branch v0.56.0 https://github.com/vmware/govmomi.git
#   (cd govmomi/vcsim && go build -o ~/go/bin/vcsim .)
# or use the image: docker run -p 8989:8989 vmware/vcsim (then --seed-only).
#
# Stop it with: kill "$GOVC_SIM_PID"
set -euo pipefail

seed_only=0
if [[ "${1:-}" == "--seed-only" ]]; then
  seed_only=1
  shift
fi
PORT="${1:-8989}"

command -v govc >/dev/null || { echo "govc not found (brew install govc)" >&2; exit 2; }

if [[ $seed_only -eq 0 ]]; then
  VCSIM="${VCSIM_BIN:-$(command -v vcsim || echo "$HOME/go/bin/vcsim")}"
  [[ -x "$VCSIM" ]] || { echo "vcsim not found (see the header of $0)" >&2; exit 2; }
  log="${TMPDIR:-/tmp}/vcsim-$PORT.log"
  # vSphere 8 API like the lab; 1 DC, 1 cluster of 4 hosts (esx01 = H0 runs the VCSA on site), 4 local datastores.
  "$VCSIM" -l "127.0.0.1:$PORT" -api-version 8.0.3.0 -dc 1 -cluster 1 -host 4 -ds 4 -pod 0 -app 0 -folder 0 \
    -pg 1 -vm 2 -standalone-host 0 >"$log" 2>&1 &
  pid=$!
  for _ in $(seq 1 50); do
    grep -q GOVC_URL "$log" 2>/dev/null && break
    sleep 0.2
  done
  export GOVC_URL="https://user:pass@127.0.0.1:$PORT/sdk"
fi

export GOVC_INSECURE=1 GOVC_DATACENTER="${GOVC_DATACENTER:-DC0}"
DC="/$GOVC_DATACENTER"
CLUSTER="$DC/host/${GOVC_DATACENTER}_C0"
DVS="${VSPHERE_RANGE_DVS:-vDS-10G}"

exists() { [[ -n "$(govc find "$DC" -name "$1" 2>/dev/null)" ]]; }

if ! exists "$DVS"; then
  govc dvs.create -folder "$DC/network" "$DVS"
  for host in $(govc find "$CLUSTER" -type h); do
    govc dvs.add -dvs "$DVS" -pnic vmnic1 "$host"
  done
fi
exists dPG-TN-MGMT || govc dvs.portgroup.add -dvs "$DVS" -type earlyBinding -vlan 30 dPG-TN-MGMT
exists dPG-TN-SVC || govc dvs.portgroup.add -dvs "$DVS" -type earlyBinding -vlan 32 dPG-TN-SVC

first_host="$(govc find "$CLUSTER" -type h | sort | head -1)"
while read -r name guest; do
  exists "$name" && continue
  govc vm.create -on=false -c 1 -m 512 -g "$guest" -net dPG-TN-MGMT -net.adapter vmxnet3 \
    -disk 1GB -ds LocalDS_0 -pool "$CLUSTER/Resources" -host "$first_host" "$name"
  govc vm.markastemplate "$name"
done <<'EOF'
tmpl-ubuntu-2404 ubuntu64Guest
tmpl-pfsense freebsd12_64Guest
tmpl-win2022 windows2019srvNext_64Guest
EOF

if [[ $seed_only -eq 0 ]]; then
  cat <<EOF
# vcsim $(govc about | awk -F': *' '/^Version/{print $2}') (govmomi simulator) on 127.0.0.1:$PORT, pid $pid
export GOVC_URL=$GOVC_URL GOVC_INSECURE=1 GOVC_SIM_PID=$pid
export VSPHERE_URL=https://127.0.0.1:$PORT VSPHERE_USERNAME=user VSPHERE_PASSWORD=pass
export VSPHERE_DATACENTER=$GOVC_DATACENTER VSPHERE_CLUSTER=${GOVC_DATACENTER}_C0 VSPHERE_RANGE_DVS=$DVS
export VSPHERE_VLAN_POOL=100-199 VSPHERE_PLACEMENT=spread VSPHERE_VERIFY_SSL=false VSPHERE_CONTENT_LIBRARY=
export VSPHERE_MGMT_NETWORK=dPG-TN-MGMT VSPHERE_EXCLUDE_HOSTS=${GOVC_DATACENTER}_C0_H0
# vcsim serves no /api/session, /api/vcenter/vm/{id}/power|tools|guest: the provisioner's
# REST half will 404 against it (see tests/integration/test_vsphere_vcsim.py).
EOF
fi
