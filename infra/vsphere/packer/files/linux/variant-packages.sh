#!/usr/bin/env bash
# TrueNorth Range — Linux variant packages (variants.pkr.hcl, variant-linux).
# Runs as root on a clone of a Linux base, before the variant scripts.
#
#   VARIANT_PACKAGES  space-separated package names (apt on Debian/Ubuntu, dnf on Rocky)
#   DEPOT_URL         TN-DEPOT01 base URL. Not used to rewrite sources here: the build
#                     network has egress, and the base's own mirrors are what a range
#                     admin would expect to see. Exported for the variant scripts.
#
# Fails the build if a package cannot be installed: a variant missing its software is
# only discovered inside a no-egress range otherwise.
set -euo pipefail

name="${VARIANT_NAME:-variant}"
read -r -a pkgs <<<"${VARIANT_PACKAGES:-}"
echo "=== variant ${name}: packages"
if [ "${#pkgs[@]}" -eq 0 ]; then
  echo "No packages requested."
  exit 0
fi

# A clone boots with cloud-init re-running (cleanup.sh cleaned it); let it finish so it
# does not hold the apt/dnf lock while we install.
if command -v cloud-init >/dev/null 2>&1; then
  cloud-init status --wait >/dev/null 2>&1 || true
fi

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  # A base built with depot_url carries a depot apt source that the build network may
  # not reach; a failed index fetch is not fatal as long as the install below succeeds.
  apt-get update || echo "WARN: apt-get update reported errors; continuing" >&2
  apt-get install -y --no-install-recommends "${pkgs[@]}"
  apt-get clean
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y "${pkgs[@]}"
  dnf clean all
else
  echo "No apt-get or dnf on this base; cannot install: ${pkgs[*]}" >&2
  exit 1
fi
echo "=== variant ${name}: ${#pkgs[@]} packages installed"
