#!/usr/bin/env bash
# TrueNorth derived image: certificate authority (step-ca) on ubuntu-lts.
set -euo pipefail
echo "=== ca-host role"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update || true
sudo apt-get install -y step-cli step-ca openssl || echo "TODO: install step-ca from depot (not in base apt)"
# Range-specific CA init (PKI material, provisioner password) is a DEPLOY-time step, not baked.
echo "TODO(deploy): 'step ca init' runs per-range so each range gets its own root/intermediate."
