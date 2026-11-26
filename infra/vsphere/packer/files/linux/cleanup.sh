#!/usr/bin/env bash
# Author: Marc-Andre Blanke
# Created: 2026-10-07T14:21:35Z
# TrueNorth Range — Linux template cleanup (stage T). Runs as the last provisioner on
# every Linux build so clones get a fresh identity under vSphere/cloud-init.
set -euo pipefail

echo "=== remove the build apt/dnf proxy (ranges pass TN_DEPOT_APT_PROXY at deploy)"
# apt-proxy.sh, Ubuntu autoinstall (curtin) and debian-installer (mirror/http/proxy) each
# leave one behind; none of them is reachable from a range.
sudo rm -f /etc/apt/apt.conf.d/90tn-build-proxy /etc/apt/apt.conf.d/90curtin-aptproxy || true
if [ -f /etc/apt/apt.conf ]; then sudo sed -i '/^Acquire::http::Proxy /d' /etc/apt/apt.conf || true; fi
if [ -f /etc/dnf/dnf.conf ]; then sudo sed -i '/^# tn-build-proxy$/,+1d' /etc/dnf/dnf.conf || true; fi

# The install DVD as an apt source (debian-installer and Kali leave "deb cdrom:" behind): a
# clone has no DVD, so apt-get update fails on it (the parrot variant and every range that
# installs a package). <root> is "" on the guest; tests pass a scratch directory.
drop_cdrom_sources() {
  local root="${1:-}" f re='^[[:space:]]*deb(-src)?[[:space:]]+(\[[^]]*\][[:space:]]+)?cdrom:'
  for f in "$root/etc/apt/sources.list" "$root"/etc/apt/sources.list.d/*.list; do
    if [ ! -f "$f" ] || ! grep -Eq "$re" "$f"; then continue; fi
    { grep -Ev "$re" "$f" || true; } | sudo tee "$f.tn-new" >/dev/null && sudo mv -f "$f.tn-new" "$f"
  done
  for f in "$root"/etc/apt/sources.list.d/*.sources; do
    [ -f "$f" ] || continue
    # deb822: drop a file whose every URIs line is the DVD.
    if grep -q '^URIs:.*cdrom:' "$f" && ! grep '^URIs:' "$f" | grep -vq 'cdrom:'; then sudo rm -f "$f"; fi
  done
}
echo "=== remove the install DVD from the apt sources"
drop_cdrom_sources ""

echo "=== cloud-init clean"
sudo cloud-init clean --logs || true

echo "=== truncate machine-id (regenerated on first boot of a clone)"
sudo truncate -s 0 /etc/machine-id || true
if [ -f /var/lib/dbus/machine-id ]; then
  sudo rm -f /var/lib/dbus/machine-id
  sudo ln -s /etc/machine-id /var/lib/dbus/machine-id || true
fi

# Regenerated on a clone's first boot by cloud-init's ssh module, else tn-ssh-hostkeys.service
# (require-cloud-init.sh checked both before this runs).
echo "=== remove SSH host keys (regenerated on first boot)"
sudo rm -f /etc/ssh/ssh_host_* || true

echo "=== clear logs, shell history, apt/dnf caches"
sudo rm -rf /tmp/* /var/tmp/* || true
sudo find /var/log -type f -exec truncate -s 0 {} \; 2>/dev/null || true
if command -v apt-get >/dev/null 2>&1; then sudo apt-get clean || true; fi
if command -v dnf >/dev/null 2>&1; then sudo dnf clean all || true; fi
cat /dev/null > ~/.bash_history 2>/dev/null || true
history -c 2>/dev/null || true

echo "=== cleanup done"
