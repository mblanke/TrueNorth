#!/usr/bin/env bash
# TrueNorth derived image: cloud log emulation (Azure/AWS log sets) on ubuntu-lts.
set -euo pipefail
echo "=== cloudlog-emu role"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update || true
sudo apt-get install -y python3 python3-venv jq || echo "TODO: pull from depot when offline"
echo "TODO(deploy): install the cloud log-set generator and seed emulated Azure/AWS logs per EO."
