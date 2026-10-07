#!/usr/bin/env bash
# TrueNorth Range — Linux template cleanup (stage T). Runs as the last provisioner on
# every Linux build so clones get a fresh identity under vSphere/cloud-init.
set -euo pipefail

echo "=== cloud-init clean"
sudo cloud-init clean --logs || true

echo "=== truncate machine-id (regenerated on first boot of a clone)"
sudo truncate -s 0 /etc/machine-id || true
if [ -f /var/lib/dbus/machine-id ]; then
  sudo rm -f /var/lib/dbus/machine-id
  sudo ln -s /etc/machine-id /var/lib/dbus/machine-id || true
fi

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
