#!/usr/bin/env bash
# TrueNorth Range — point apt / dnf at the build proxy (TN-DEPOT01 apt-cacher-ng) for the
# rest of a Packer build. Runs on every Linux base, derived image and variant before any
# package step; cleanup.sh removes what this writes, so templates never ship a proxy.
#
#   APT_PROXY  http://host:port (Packer variable apt_proxy), e.g. http://10.30.31.30:3142.
#              Empty: nothing is configured and the build stays offline / as before.
#
# apt-cacher-ng is a FORWARD proxy: guests keep their stock http:// sources and send the
# full upstream URL. It refuses CONNECT tunnels (install/lab/roles/tn_depot/templates/
# acng.conf.j2), so only http:// repositories go through it. Rocky's stock repos use an
# https mirrorlist; they are switched to the http baseurl on dl.rockylinux.org, the same
# change the depot prefetch makes (tn_prefetch/templates/prefetch_linux.sh.j2), and that
# change stays in the template so range guests hit the cache too.
set -euo pipefail

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"
R="${TN_TEST_ROOT:-}"   # tests only: a scratch directory standing in for /
proxy="${APT_PROXY:-}"
if [ -z "$proxy" ]; then
  echo "apt-proxy: APT_PROXY empty; no build proxy"
  exit 0
fi
case "$proxy" in
  http://*) ;;
  *) echo "apt-proxy: APT_PROXY must be http://host:port, got '$proxy'" >&2; exit 1 ;;
esac

if command -v apt-get >/dev/null 2>&1; then
  printf 'Acquire::http::Proxy "%s";\n' "$proxy" | $SUDO tee "$R/etc/apt/apt.conf.d/90tn-build-proxy" >/dev/null
  echo "apt-proxy: apt -> $proxy (/etc/apt/apt.conf.d/90tn-build-proxy)"
fi
if command -v dnf >/dev/null 2>&1; then
  for repo in "$R"/etc/yum.repos.d/*.repo; do
    [ -e "$repo" ] || continue
    $SUDO sed -i -e 's|^mirrorlist=|#mirrorlist=|' \
      -e 's|^#baseurl=http://dl.rockylinux.org|baseurl=http://dl.rockylinux.org|' \
      -e 's|^#baseurl=https://dl.rockylinux.org|baseurl=http://dl.rockylinux.org|' "$repo"
  done
  # dnf.conf has no inline comments: a marker line, then the setting (stock dnf.conf has
  # only [main], so appending lands there). cleanup.sh deletes the pair.
  $SUDO sed -i '/^# tn-build-proxy$/,+1d' "$R/etc/dnf/dnf.conf"
  printf '# tn-build-proxy\nproxy=%s\n' "$proxy" | $SUDO tee -a "$R/etc/dnf/dnf.conf" >/dev/null
  echo "apt-proxy: dnf -> $proxy (/etc/dnf/dnf.conf, repos on http baseurl)"
fi
