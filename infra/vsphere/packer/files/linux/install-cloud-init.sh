#!/usr/bin/env bash
# TrueNorth Range - cloud-init on every Linux template (Debian/Ubuntu family; Rocky gets it
# from its kickstart %packages). The worker configures a Linux range VM ONLY through
# cloud-init's VMware datasource (guestinfo.metadata / guestinfo.userdata: hostname, static
# IPs, accounts; vsphere_api.py _customize_sync). It never runs vSphere Linux guest
# customization, so a template without cloud-init gives its clones no IP and no accounts.
#
# Sources, in order; never the internet unless the build already has a mirror/apt proxy:
#   1. already installed (Ubuntu, Debian DVD preseed, Kali/Parrot late_command)
#   2. the configured mirror or build apt proxy (apt_proxy -> TN-DEPOT01 apt-cacher-ng)
#   3. the install ISO, still attached to the build VM (Kali "everything", Debian DVD pools)
# None of them: exit 1, so the build fails and no template is made.
set -uo pipefail
have() { command -v cloud-init >/dev/null 2>&1; }
if have; then echo "cloud-init: already installed ($(cloud-init --version 2>&1))"; exit 0; fi
command -v apt-get >/dev/null 2>&1 || { echo "cloud-init: not installed and no apt-get" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo "=== cloud-init from the configured sources"
sudo -E apt-get install -y cloud-init && have && exit 0

echo "=== cloud-init from the install ISO"
MNT=/media/tn-iso
LIST=/etc/apt/sources.list.d/tn-iso.list
sudo mkdir -p "$MNT"
for dev in /dev/sr0 /dev/sr1 /dev/sr2 /dev/sr3; do
  [ -b "$dev" ] || continue
  sudo mount -o ro "$dev" "$MNT" 2>/dev/null || continue
  if [ -d "$MNT/dists" ] && [ -d "$MNT/pool" ]; then
    for suite_dir in "$MNT"/dists/*; do
      [ -d "$suite_dir" ] && [ ! -L "$suite_dir" ] || continue
      suite="${suite_dir##*/}"
      comps="$(find "$suite_dir" -mindepth 1 -maxdepth 1 -type d -printf '%f ' 2>/dev/null)"
      [ -n "$comps" ] || continue
      echo "deb [trusted=yes] file:$MNT $suite $comps" | sudo tee "$LIST" >/dev/null
      if sudo -E apt-get update -o Dir::Etc::sourcelist="$LIST" -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0 \
        && sudo -E apt-get install -y cloud-init; then
        break
      fi
    done
  fi
  sudo rm -f "$LIST"
  sudo umount "$MNT" 2>/dev/null || true
  have && break
done
sudo rm -f "$LIST"
sudo -E apt-get update >/dev/null 2>&1 || true
if have; then echo "cloud-init: installed from the ISO ($(cloud-init --version 2>&1))"; exit 0; fi
echo "cloud-init: NOT installed (no mirror/apt proxy has it and the ISO pool does not)." >&2
echo "Build with the depot (pkrvars-work.py --depot) so apt_proxy can fetch it. No template." >&2
exit 1
