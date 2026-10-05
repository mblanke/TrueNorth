#!/usr/bin/env bash
# TrueNorth lab - clone TN-BUILD01 or TN-DEPOT01 from tmpl-ubuntu-2404 (runbook §4.1a).
#
#   scripts/lab/deploy-mgmt-vm.sh TN-BUILD01 --ssh-key ~/.ssh/id_ed25519_lab.pub           # plan
#   scripts/lab/deploy-mgmt-vm.sh TN-BUILD01 --ssh-key ~/.ssh/id_ed25519_lab.pub --apply
#   scripts/lab/deploy-mgmt-vm.sh TN-DEPOT01 --ssh-key ~/.ssh/id_ed25519_lab.pub --data-disk 2T --apply
#
# Options:
#   --ssh-key PATH     PUBLIC key for user tnadmin (required; a private key is refused)
#   --host esx0N       target host (default: the one of esx02-04 with the most free RAM).
#                      esx01 holds the VCSA and is always refused.
#   --data-disk SIZE   TN-DEPOT01 only: extra thin data disk, mounted at /srv/depot (default 1T)
#   --no-uplink        TN-DEPOT01 only: no second NIC on dPG-TN-BUILD (see below)
#   --template NAME    default tmpl-ubuntu-2404
#   --apply            actually change vCenter (default: plan only, read-only)
#
#   VM          NIC 0 (port group / IP)               vCPU  RAM    disk
#   TN-BUILD01  dPG-TN-BUILD 10.30.31.10/24 gw .1     8     16 GB  200 GB
#   TN-DEPOT01  dPG-TN-SVC   10.30.32.10/24 gw .1     4     16 GB  100 GB + data disk
#               NIC 1: dPG-TN-BUILD 10.30.31.11/24, created DISCONNECTED. It is the depot's
#               only path to the internet and is connected only for a prefetch window
#               (scripts/lab/depot-uplink.sh). Replies from 10.30.32.10 are policy-routed
#               back out NIC 0, so the default route on NIC 1 never carries range traffic.
#
# The clone goes on the chosen host's own local datastore (esx0N-local). cloud-init gets
# its config through guestinfo.metadata / guestinfo.userdata (VMware datasource): static
# IP, hostname, user tnadmin with the given key and passwordless sudo, no password login.
# guestinfo is readable by anyone with read access to the VM in vCenter, so it carries
# nothing secret - only the public key.
#
# Idempotent: if the VM already exists it is reported and left alone.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

NAME=""; SSH_KEY=""; HOST_ARG=""; DATA_DISK="1T"; UPLINK=1; TEMPLATE="tmpl-ubuntu-2404"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --ssh-key) SSH_KEY="$2"; shift ;;
    --host) HOST_ARG="$2"; shift ;;
    --data-disk) DATA_DISK="$2"; shift ;;
    --no-uplink) UPLINK=0 ;;
    --template) TEMPLATE="$2"; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown option: $1" >&2; usage 2 ;;
    *) [[ -z "$NAME" ]] || usage 2; NAME="$1" ;;
  esac
  shift
done

case "$NAME" in
  TN-BUILD01) CPU=8; MEM_MB=16384; OS_DISK=200G; PG=dPG-TN-BUILD; IP=10.30.31.10; GW=10.30.31.1
              DNS=10.30.31.1; HN=tn-build01; DATA_DISK=""; UPLINK=0 ;;
  TN-DEPOT01) CPU=4; MEM_MB=16384; OS_DISK=100G; PG=dPG-TN-SVC;   IP=10.30.32.10; GW=10.30.32.1
              DNS=10.30.31.1; HN=tn-depot01
              UPLINK_PG=dPG-TN-BUILD; UPLINK_IP=10.30.31.11; UPLINK_GW=10.30.31.1 ;;
  "") usage 2 ;;
  *) die "unknown VM '$NAME' (TN-BUILD01 or TN-DEPOT01)" ;;
esac
PREFIX=24

[[ -n "$SSH_KEY" ]] || die "--ssh-key PATH (the PUBLIC key for tnadmin) is required"
[[ -f "$SSH_KEY" ]] || die "no such file: $SSH_KEY"
grep -q 'PRIVATE KEY' "$SSH_KEY" && die "$SSH_KEY is a private key. Pass the .pub file"
PUBKEY="$(head -n1 "$SSH_KEY")"
[[ "$PUBKEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh.com)\ [A-Za-z0-9+/=]+ ]] \
  || die "$SSH_KEY does not look like an OpenSSH public key"
need base64 "coreutils"

step "Preflight"
lab_preflight

existing="$(govc find / -type m -name "$NAME")"
if [[ -n "$existing" ]]; then
  step "$NAME already exists - leaving it alone"
  govc vm.info -r "$existing" | sed 's/^/  /'
  govc vm.ip -v4 -wait 5s "$existing" 2>/dev/null | sed 's/^/  ip: /' || true
  exit 0
fi

tpl="$(govc find / -type m -name "$TEMPLATE")"
[[ -n "$tpl" && "$(wc -l <<<"$tpl" | tr -d ' ')" == "1" ]] || die "expected one VM/template named $TEMPLATE, found: ${tpl:-none}"
info "template: $tpl"
for pg in "$PG" ${UPLINK_PG:+"$UPLINK_PG"}; do
  [[ -n "$(govc find / -type g -name "$pg")" ]] || die "port group $pg does not exist. Run scripts/lab/create-portgroups.sh --apply first"
done
CLUSTER_PATH="$(govc find / -type c -name "$TN_CLUSTER")"
[[ -n "$CLUSTER_PATH" ]] || die "cluster $TN_CLUSTER not found"
POOL="$CLUSTER_PATH/Resources"

step "Host"
best=""; best_free=-1
while IFS= read -r h; do
  [[ -n "$h" ]] || continue
  short="${h##*/}"
  if is_forbidden_host "$short"; then info "skip  $short (holds the VCSA)"; continue; fi
  if [[ -n "$HOST_ARG" && "${short%%.*}" != "${HOST_ARG%%.*}" ]]; then continue; fi
  conn="$(govc object.collect -s "$h" runtime.connectionState)"
  maint="$(govc object.collect -s "$h" runtime.inMaintenanceMode)"
  size="$(govc object.collect -s "$h" summary.hardware.memorySize)"
  used="$(govc object.collect -s "$h" summary.quickStats.overallMemoryUsage)"
  free=$(( size / 1048576 - used ))
  if [[ "$conn" != "connected" || "$maint" != "false" ]]; then info "skip  $short ($conn, maintenance=$maint)"; continue; fi
  info "      $short free RAM ${free} MB"
  if (( free > best_free )); then best="$h"; best_free=$free; fi
done < <(govc find "$CLUSTER_PATH" -type h)
if [[ -n "$HOST_ARG" ]] && is_forbidden_host "$HOST_ARG"; then die "refusing --host $HOST_ARG: esx01 holds the VCSA"; fi
[[ -n "$best" ]] || die "no eligible host${HOST_ARG:+ matching $HOST_ARG}"
HOST_PATH="$best"; HOST_SHORT="${best##*/}"; HOST_SHORT="${HOST_SHORT%%.*}"
is_forbidden_host "$HOST_SHORT" && die "refusing $HOST_SHORT"
DS="${HOST_SHORT}-local"
[[ -n "$(govc find / -type s -name "$DS")" ]] || die "datastore $DS not found"
info "chosen: $HOST_SHORT, datastore $DS"

# --- cloud-init -----------------------------------------------------------------
render_metadata() { # $1 = MAC of NIC 0, $2 = MAC of NIC 1 (or empty)
  cat <<EOF
instance-id: ${HN}-$(date -u +%Y%m%d%H%M%S)
local-hostname: ${HN}
network:
  version: 2
  ethernets:
    nic0:
      match: {macaddress: "$1"}
      addresses: [${IP}/${PREFIX}]
EOF
  if [[ -z "${2:-}" ]]; then
    cat <<EOF
      routes: [{to: default, via: ${GW}}]
      nameservers: {addresses: [${DNS}]}
EOF
  else
    # Depot: NIC 0 carries no main-table default route. Anything sourced from the SVC
    # address goes back out NIC 0 (table 32), so ranges and BUILD01 get symmetric
    # replies whether the uplink is connected or not. Main-table default = uplink only.
    cat <<EOF
      routes:
        - {to: default, via: ${GW}, table: 32}
        - {to: ${IP%.*}.0/${PREFIX}, scope: link, table: 32}
      routing-policy:
        - {from: ${IP}, table: 32}
    nic1:
      match: {macaddress: "$2"}
      optional: true
      addresses: [${UPLINK_IP}/${PREFIX}]
      routes: [{to: default, via: ${UPLINK_GW}}]
      nameservers: {addresses: [${DNS}]}
EOF
  fi
}

render_userdata() {
  cat <<EOF
#cloud-config
hostname: ${HN}
preserve_hostname: false
users:
  - name: tnadmin
    gecos: TrueNorth lab admin
    groups: [sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: true
    ssh_authorized_keys:
      - "${PUBKEY}"
disable_root: true
ssh_pwauth: false
package_update: false
EOF
  if [[ -n "$DATA_DISK" ]]; then
    # Second disk = /dev/sdb on the template's SCSI controller. overwrite:false never
    # reformats a disk that already has a filesystem.
    cat <<'EOF'
fs_setup:
  - {label: depot-data, filesystem: ext4, device: /dev/sdb, partition: none, overwrite: false}
mounts:
  - [LABEL=depot-data, /srv/depot, ext4, "defaults,nofail,x-systemd.device-timeout=60s", "0", "2"]
EOF
  fi
}

step "Plan for $NAME"
info "clone $TEMPLATE -> $NAME on $HOST_SHORT / $DS, ${CPU} vCPU, $((MEM_MB/1024)) GB, OS disk ${OS_DISK}"
info "NIC 0: $PG, ${IP}/${PREFIX} gw ${GW}"
[[ -n "${UPLINK_PG:-}" && "$UPLINK" == "1" ]] && info "NIC 1: $UPLINK_PG, ${UPLINK_IP}/${PREFIX} gw ${UPLINK_GW}, DISCONNECTED (depot-uplink.sh)"
[[ -n "$DATA_DISK" ]] && info "data disk: ${DATA_DISK} thin -> /srv/depot"
info "cloud-init user-data:"
render_userdata | sed 's/^/    | /'

act govc vm.clone -vm "$tpl" -on=false -host "$HOST_PATH" -ds "$DS" -pool "$POOL" \
    -c "$CPU" -m "$MEM_MB" -annotation "TrueNorth lab ${NAME} (scripts/lab/deploy-mgmt-vm.sh)" "$NAME"
if [[ "$APPLY" != "1" ]]; then
  cat <<EOF
  [plan] remove the clone's inherited vApp/OVF config (vim_helper.py vapp-off), if any
  [plan] govc vm.disk.change -vm $NAME -disk.name <first disk> -size $OS_DISK
EOF
  [[ -n "$DATA_DISK" ]] && echo "  [plan] govc vm.disk.create -vm $NAME -name $NAME/${NAME}_data -size $DATA_DISK -ds $DS"
  echo "  [plan] govc vm.network.change -vm $NAME -net $PG ethernet-0"
  [[ -n "${UPLINK_PG:-}" && "$UPLINK" == "1" ]] && echo "  [plan] govc vm.network.add -vm $NAME -net $UPLINK_PG -net.adapter vmxnet3; govc device.disconnect -vm $NAME ethernet-1"
  echo "  [plan] set guestinfo.metadata / guestinfo.userdata (base64), power on, wait for ${IP}"
  echo; echo "Plan only. Re-run with --apply to change vCenter."
  exit 0
fi

VM="$(govc find / -type m -name "$NAME")"
[[ -n "$VM" ]] || die "clone finished but $NAME is not in the inventory"

# OVF datasource would win over the VMware datasource (see vim_helper.py).
has_vapp="$(govc vm.info -json "$VM" | jq "$JQ_CI"' [ci("vAppConfig")[] | select(. != null)] | length')"
if [[ "$has_vapp" != "0" ]]; then vim_helper vapp-off --vm "$NAME"; else info "ok    no vApp/OVF config on the clone"; fi

disk0="$(govc device.ls -vm "$VM" | awk '/^disk-/{print $1; exit}')"
[[ -n "$disk0" ]] || die "clone has no disk"
act govc vm.disk.change -vm "$VM" -disk.name "$disk0" -size "$OS_DISK"
[[ -n "$DATA_DISK" ]] && act govc vm.disk.create -vm "$VM" -name "$NAME/${NAME}_data" -size "$DATA_DISK" -ds "$DS"

nic0="$(govc device.ls -vm "$VM" | awk '/^ethernet-/{print $1; exit}')"
[[ -n "$nic0" ]] || die "$TEMPLATE has no network adapter"
act govc vm.network.change -vm "$VM" -net "$PG" "$nic0"
nic1=""
if [[ -n "${UPLINK_PG:-}" && "$UPLINK" == "1" ]]; then
  act govc vm.network.add -vm "$VM" -net "$UPLINK_PG" -net.adapter vmxnet3
  nic1="$(govc device.ls -vm "$VM" | awk '/^ethernet-/{print $1}' | grep -vx "$nic0" | head -n1)"
  act govc device.disconnect -vm "$VM" "$nic1"
  start="$(govc device.info -json -vm "$VM" "$nic1" | jq -r "$JQ_CI"' ci("startConnected")[0]')"
  [[ "$start" == "false" ]] || die "$nic1 is still set to connect at power-on; disconnect it before powering on"
fi

mac() { govc device.info -json -vm "$VM" "$1" | jq -r "$JQ_CI"' ci("macAddress")[0]'; }
MAC0="$(mac "$nic0")"; MAC1=""; [[ -n "$nic1" ]] && MAC1="$(mac "$nic1")"
info "MACs: $nic0=$MAC0${nic1:+ $nic1=$MAC1}"

b64() { base64 | tr -d '\n'; }
act govc vm.change -vm "$VM" \
    -e "guestinfo.metadata=$(render_metadata "$MAC0" "$MAC1" | b64)" -e guestinfo.metadata.encoding=base64 \
    -e "guestinfo.userdata=$(render_userdata | b64)" -e guestinfo.userdata.encoding=base64

act govc vm.power -on "$VM"
step "Waiting for $IP (up to 10 min)"
deadline=$(( $(date +%s) + 600 ))
while :; do
  # -n: NIC 0 only. -a would wait forever for the depot's disconnected uplink NIC.
  ips="$(govc vm.ip -v4 -n "$nic0" -wait 30s "$VM" 2>/dev/null || true)"
  if grep -qw "$IP" <<<"$ips"; then info "up: $ips"; break; fi
  (( $(date +%s) < deadline )) || die "$NAME did not report $IP (got: ${ips:-nothing}). Check the console: cloud-init status --long"
done

cat <<EOF

RESULT: $NAME on $HOST_SHORT / $DS, ${IP}. Log in: ssh -i <private key> tnadmin@${IP}
EOF
if [[ -n "$nic1" ]]; then
  echo "Uplink NIC $nic1 is DISCONNECTED. Connect it for the depot install and prefetch:"
  echo "  scripts/lab/depot-uplink.sh on --apply   ...   scripts/lab/depot-uplink.sh off --apply"
fi
