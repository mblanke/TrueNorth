#!/bin/bash
# TrueNorth common Linux guest configuration. Runs through VMware Tools as root.
# Inputs (environment):
#   TN_HOSTNAME, TN_ROLE, TN_INSTANCE, TN_NIC_COUNT
#   TN_NIC_<n>="<mac>|<ip>/<prefix>|<gateway or empty>"
#   TN_VAR_<KEY>=<value>  (from the range definition's vm vars)
# Matches NICs by MAC address, never by name or order.
# LAB-VALIDATE on each template OS before relying on it.
set -euo pipefail
log() { echo "[tn-common] $*"; }

[ -n "${TN_HOSTNAME:-}" ] || { echo "TN_HOSTNAME missing" >&2; exit 10; }
count="${TN_NIC_COUNT:-0}"

iface_for_mac() {
  local mac want="${1,,}"
  for p in /sys/class/net/*; do
    mac=$(cat "$p/address" 2>/dev/null || true)
    [ "${mac,,}" = "$want" ] && { basename "$p"; return 0; }
  done
  return 1
}

# ---- VyOS router ----------------------------------------------------------
if [ -x /opt/vyatta/sbin/vyatta-cfg-cmd-wrapper ]; then
  W=/opt/vyatta/sbin/vyatta-cfg-cmd-wrapper
  $W begin
  $W set system host-name "$TN_HOSTNAME"
  for ((i = 0; i < count; i++)); do
    var="TN_NIC_$i"; IFS='|' read -r mac addr gw <<<"${!var}"
    ifc=$(iface_for_mac "$mac") || { echo "no interface for $mac" >&2; $W end; exit 11; }
    $W delete interfaces ethernet "$ifc" address || true
    $W set interfaces ethernet "$ifc" address "$addr"
    $W set interfaces ethernet "$ifc" description "tn-$TN_INSTANCE"
  done
  $W commit
  $W save
  $W end
  log "vyos configured: $TN_HOSTNAME"
  exit 0
fi

# ---- General Linux --------------------------------------------------------
hostnamectl set-hostname "$TN_HOSTNAME" 2>/dev/null || echo "$TN_HOSTNAME" > /etc/hostname
sed -i "/^127\.0\.1\.1[[:space:]]/d" /etc/hosts
echo "127.0.1.1 $TN_HOSTNAME" >> /etc/hosts

if command -v netplan >/dev/null 2>&1; then
  f=/etc/netplan/60-truenorth.yaml
  {
    echo "network:"; echo "  version: 2"; echo "  ethernets:"
    for ((i = 0; i < count; i++)); do
      var="TN_NIC_$i"; IFS='|' read -r mac addr gw <<<"${!var}"
      echo "    tn$i:"
      echo "      match: {macaddress: \"${mac,,}\"}"
      echo "      set-name: tn$i"
      echo "      dhcp4: false"
      echo "      dhcp6: false"
      echo "      addresses: [\"$addr\"]"
      [ -n "$gw" ] && echo "      routes: [{to: default, via: \"$gw\"}]"
    done
  } > "$f"
  chmod 600 "$f"
  # Remove cloud-init / installer defaults that would add DHCP on the same NICs.
  for other in /etc/netplan/*.yaml; do
    [ "$other" = "$f" ] && continue
    mv "$other" "$other.tn-disabled"
  done
  netplan apply
elif command -v nmcli >/dev/null 2>&1; then
  for ((i = 0; i < count; i++)); do
    var="TN_NIC_$i"; IFS='|' read -r mac addr gw <<<"${!var}"
    ifc=$(iface_for_mac "$mac") || { echo "no interface for $mac" >&2; exit 11; }
    nmcli -t -f NAME,DEVICE con show | awk -F: -v d="$ifc" '$2==d{print $1}' | while read -r c; do nmcli con delete "$c"; done
    args=(con add type ethernet ifname "$ifc" con-name "tn$i" ipv4.method manual ipv4.addresses "$addr" ipv6.method disabled)
    [ -n "$gw" ] && args+=(ipv4.gateway "$gw")
    nmcli "${args[@]}"
    nmcli con up "tn$i"
  done
else
  echo "no supported network manager (netplan or nmcli) found" >&2
  exit 12
fi

# Pristine: clear identity that must not be shared between clones.
truncate -s 0 /etc/machine-id 2>/dev/null || true
rm -f /var/lib/dbus/machine-id && ln -sf /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true
systemd-machine-id-setup >/dev/null 2>&1 || true

log "configured $TN_HOSTNAME ($TN_ROLE) with $count NIC(s)"
