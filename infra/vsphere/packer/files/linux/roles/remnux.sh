#!/usr/bin/env bash
# TrueNorth derived image: REMnux (malware analysis) on ubuntu-lts.
set -euo pipefail
echo "=== remnux role"
# REMnux ships an official installer. Online-only; run from the depot mirror when offline.
if command -v curl >/dev/null 2>&1 && curl -fsSL https://REMnux.org >/dev/null 2>&1; then
  wget -qO /tmp/remnux-install https://REMnux.org/remnux-cli
  chmod +x /tmp/remnux-install
  sudo /tmp/remnux-install install || echo "TODO: REMnux installer failed; retry from depot mirror"
else
  echo "TODO(offline): mirror the REMnux installer to the depot and run it here."
fi
