#!/usr/bin/env bash
# Author: Marc-Andre Blanke
# Created: 2026-11-23T20:47:00Z
# Variant script (kali-gui): boot to the graphical login. Runs as root after the packages
# (kali-desktop-xfce, open-vm-tools-desktop) are installed. Fails the build if no display
# manager is present, so a "GUI" template can never ship without one.
set -euo pipefail

if ! systemctl list-unit-files lightdm.service --no-legend | grep -q lightdm; then
  echo "kali-gui: lightdm is not installed (kali-desktop-xfce did not bring it)" >&2
  exit 1
fi
echo "/usr/sbin/lightdm" > /etc/X11/default-display-manager
systemctl enable lightdm.service
systemctl set-default graphical.target
echo "kali-gui: default target $(systemctl get-default), display manager lightdm"
