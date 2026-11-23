#!/usr/bin/env bash
# Author: Marc-Andre Blanke
# Created: 2026-11-23T20:47:00Z
# Variant script (parrot): convert a debian13 clone to Parrot Security 7 (echo).
# Runs as root after files/linux/apt-proxy.sh, so apt goes through the build apt proxy
# (TN-DEPOT01; its Remap-tnparrot serves deb.parrot.sh from an HTTPS mirror). Steps:
#   1. bootstrap the archive key: install parrot-archive-keyring from the repository once
#      with [trusted=yes] (trust on first use, over the depot), then replace that line with
#      a signed-by entry, so the unsigned trust is never kept;
#   2. install parrot-core (Parrot's apt pinning and base), then full-upgrade to Parrot;
#   3. install the MATE desktop and the full toolset, boot to the LightDM login.
# Fails the build on any error, and if the result is not Parrot or has no display manager.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
LIST=/etc/apt/sources.list.d/parrot.list
COMPONENTS="main contrib non-free non-free-firmware"

# A debian13 template built before cleanup.sh dropped the DVD source still lists it, and
# apt-get update fails on it.
sed -i "/^deb cdrom:/d" /etc/apt/sources.list 2>/dev/null || true
rm -f /etc/apt/sources.list.d/*cdrom* 2>/dev/null || true
echo "deb [trusted=yes] http://deb.parrot.sh/parrot echo ${COMPONENTS}" > "$LIST"
apt-get update -q
apt-get install "${APT_OPTS[@]}" parrot-archive-keyring
KEY=""
for k in /usr/share/keyrings/parrot-archive-keyring.gpg /etc/apt/trusted.gpg.d/parrot-archive-keyring.gpg; do
  [ -f "$k" ] && { KEY="$k"; break; }
done
[ -n "$KEY" ] || { echo "parrot: parrot-archive-keyring installed no keyring file" >&2; exit 1; }
cat > "$LIST" <<EOF
deb [signed-by=${KEY}] http://deb.parrot.sh/parrot echo ${COMPONENTS}
deb [signed-by=${KEY}] http://deb.parrot.sh/parrot echo-security ${COMPONENTS}
deb [signed-by=${KEY}] http://deb.parrot.sh/parrot echo-updates ${COMPONENTS}
EOF
echo "parrot: repository signed by ${KEY}"

apt-get update -q
apt-get install "${APT_OPTS[@]}" parrot-core
apt-get update -q
apt-get full-upgrade "${APT_OPTS[@]}"
apt-get install "${APT_OPTS[@]}" parrot-interface parrot-desktop-mate parrot-tools-full open-vm-tools-desktop
apt-get autoremove -y
apt-get clean

grep -qi parrot /etc/os-release || { echo "parrot: /etc/os-release is not Parrot after the upgrade" >&2; cat /etc/os-release >&2; exit 1; }
systemctl list-unit-files lightdm.service --no-legend | grep -q lightdm || { echo "parrot: lightdm missing" >&2; exit 1; }
echo "/usr/sbin/lightdm" > /etc/X11/default-display-manager
systemctl enable lightdm.service
systemctl set-default graphical.target
echo "parrot: $(sed -n 's/^PRETTY_NAME=//p' /etc/os-release | tr -d '"'), default target $(systemctl get-default)"
