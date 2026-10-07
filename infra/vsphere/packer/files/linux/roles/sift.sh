#!/usr/bin/env bash
# TrueNorth derived image: SANS SIFT (DFIR) on ubuntu-lts.
set -euo pipefail
echo "=== sift role"
# SIFT is installed via the 'cast' tool. Online-only; mirror to depot when offline.
if command -v curl >/dev/null 2>&1 && curl -fsSL https://github.com >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/teamdfir/sift-cli/main/install.sh -o /tmp/sift-install.sh || true
  sudo bash /tmp/sift-install.sh -i -s || echo "TODO: SIFT cast failed; retry from depot mirror"
else
  echo "TODO(offline): mirror the SIFT 'cast' installer to the depot and run it here."
fi
