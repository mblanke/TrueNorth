#!/usr/bin/env bash
# TrueNorth derived image: noise-floor user simulation (GHOSTS server) on ubuntu-lts.
set -euo pipefail
echo "=== usersim role"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update || true
sudo apt-get install -y docker.io || echo "TODO: install Docker from depot when offline"
sudo systemctl enable docker 2>/dev/null || true
# CMU SEI GHOSTS runs as containers; images must be mirrored to the depot registry.
echo "TODO(deploy): docker compose up GHOSTS (ghosts-api + grafana) from depot registry images."
