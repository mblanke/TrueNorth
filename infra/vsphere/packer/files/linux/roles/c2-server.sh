#!/usr/bin/env bash
# TrueNorth derived image: C2 teamserver (open source: Sliver + Mythic) on ubuntu-lts.
# DISABLED in the catalogue (enabled=no): building/deploying this requires the
# instructor/Standards sign-off recorded in vm_iso_catalogue.csv. Included for completeness.
set -euo pipefail
echo "=== c2-server role (SIGN-OFF REQUIRED before use)"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update || true
sudo apt-get install -y docker.io curl || echo "TODO: install from depot when offline"
echo "TODO(signed-off only): install Sliver (gh release) and Mythic (docker). Export-controlled;"
echo "                       restrict deploy to instructor roles. Cobalt Strike is a SEPARATE"
echo "                       licensed image (c2-server-cs), prod only, key applied at role-snapshot."
