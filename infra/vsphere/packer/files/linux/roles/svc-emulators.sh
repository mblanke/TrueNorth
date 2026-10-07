#!/usr/bin/env bash
# TrueNorth derived image: internet-service emulation (DNS/NTP/mail/web) on ubuntu-lts.
set -euo pipefail
echo "=== svc-emulators role"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update || true
sudo apt-get install -y bind9 bind9utils postfix nginx chrony || echo "TODO: pull from depot when offline"
# Keep services disabled in the template; range deploy supplies zones/config and enables them.
for s in named postfix nginx chrony; do sudo systemctl disable "$s" 2>/dev/null || true; done
echo "TODO(deploy): drop range-specific zone files, mail domains, web roots, then enable."
