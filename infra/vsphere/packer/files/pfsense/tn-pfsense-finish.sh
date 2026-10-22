#!/bin/sh
# TrueNorth Range - finish the pfSense template inside the installer (Packer "pfsense").
#
# boot_command leaves the installer's final dialog through Shell, mounts the build's second
# CD (label TNPFSENSE) on /tmp/tncd and runs this script from it. It replaces the hand
# finish of vm-build-guide.md (pfSense):
#   1. find the just-installed system: /mnt (installer shell), / (a chroot shell into the
#      new system), else import the ZFS pool "pfSense" at /mnt
#   2. check the offline Open-VM-Tools package set against pkgs.lock (sha256, install
#      order) and pkg-static add it into the new system; vmtoolsd must then exist
#   3. install tn-pfsense-config (/usr/local/sbin) and tn-vmtools.sh (/usr/local/etc/rc.d)
#   4. write the template's config.xml: tn-pfsense-config template tn-admin.hash
#   5. leave /cf/conf/tn-template-finish.txt, then power off; Packer templates the VM
# Any failure: say so and do NOT power off. With communicator "none" the build then fails
# at shutdown_timeout and no template is made, instead of a template that cannot take its
# per-range config. On the CD: pkgs.lock (files/pfsense, committed), the .pkg files it
# lists (staged into stage/pfsense by scripts/lab/stage_pfsense_pkgs.py), this script,
# tn-pfsense-config, tn-vmtools.sh, and tn-admin.hash (the build's ssh_password_hash).
set -u
CD="$(cd "$(dirname "$0")" && pwd)"
LOCK="$CD/pkgs.lock"
PKGS="$CD"   # the staged .pkg files sit at the CD root, next to pkgs.lock
WANT_VERSION="2.7.2"
MNT="${TN_FINISH_MNT:-/mnt}"   # test seam only (tests/contracts/test_packer_appliance_finish.py)

log() { echo "tn-finish: $*"; }
die() {
  log "FAILED: $*"
  log "NOT powering off: the Packer build fails at shutdown_timeout and makes no template."
  exit 1
}

# ── 1. the installed system ───────────────────────────────────────────
installed() { [ -f "$1/etc/version" ] && [ -x "$1/usr/local/bin/php" ] && [ -f "$1/conf.default/config.xml" ]; }
T=""
if installed "$MNT"; then
  T="$MNT"
elif df / 2>/dev/null | tail -n 1 | awk '{print $1}' | grep -q '/ROOT/' && installed ""; then
  T=""   # a chroot shell in the new system (its / is a ZFS boot environment)
else
  log "installed system not mounted; importing ZFS pool pfSense at $MNT"
  zpool import -f -N -R "$MNT" pfSense 2>/dev/null
  zfs mount pfSense/ROOT/default 2>/dev/null
  zfs mount -a 2>/dev/null
  installed "$MNT" || die "cannot find the installed pfSense (not at $MNT, not /, no pool pfSense)"
  T="$MNT"
fi
run() { if [ -n "$T" ]; then chroot "$T" "$@"; else "$@"; fi; }
VERSION="$(cat "$T/etc/version")"
case "$VERSION" in
  "$WANT_VERSION"*) ;;
  *) die "installed pfSense is $VERSION; pkgs.lock is for $WANT_VERSION" ;;
esac
log "installed system: ${T:-/} (pfSense $VERSION)"

DEVFS=0
if [ -n "$T" ] && [ ! -e "$T/dev/null" ]; then
  mount -t devfs devfs "$T/dev" || die "cannot mount devfs on $T/dev"
  DEVFS=1
fi

# ── 2. Open-VM-Tools, offline, sha256-checked ─────────────────────────
[ -s "$LOCK" ] || die "no pkgs.lock on the CD"
mkdir -p "$T/var/tmp/tn-pkgs" || die "cannot create $T/var/tmp/tn-pkgs"
ORDER=""
# shellcheck disable=SC2013 # lock fields hold no spaces: word-splitting on purpose
for entry in $(grep -v '^[[:space:]]*#' "$LOCK" | awk 'NF == 2 {print $1 "|" $2}'); do
  sum="${entry%%|*}"
  file="${entry#*|}"
  [ -f "$PKGS/$file" ] || die "$file is in pkgs.lock but not on the CD (stage it: scripts/lab/stage_pfsense_pkgs.py)"
  [ "$(sha256 -q "$PKGS/$file")" = "$sum" ] || die "sha256 mismatch: $file"
  cp "$PKGS/$file" "$T/var/tmp/tn-pkgs/" || die "cannot copy $file"
  ORDER="$ORDER $file"
done
[ -n "$ORDER" ] || die "pkgs.lock lists no packages"
for file in $ORDER; do
  name="${file%-*}"   # <name>-<version>.pkg
  if run /usr/local/sbin/pkg-static info -e "$name"; then
    log "$name already installed (pfSense base)"
    continue
  fi
  log "pkg add $file"
  run env ASSUME_ALWAYS_YES=yes IGNORE_OSVERSION=yes /usr/local/sbin/pkg-static add "/var/tmp/tn-pkgs/$file" \
    || die "pkg-static add $file"
done
rm -rf "$T/var/tmp/tn-pkgs"
[ -x "$T/usr/local/bin/vmtoolsd" ] || die "vmtoolsd not installed by the package set"

# ── 3. the boot script and the Tools rc script ────────────────────────
put_exec() { cp "$1" "$2" || return 1; chmod 0755 "$2"; }
put_exec "$CD/tn-pfsense-config" "$T/usr/local/sbin/tn-pfsense-config" || die "cannot install tn-pfsense-config"
mkdir -p "$T/usr/local/etc/rc.d"
put_exec "$CD/tn-vmtools.sh" "$T/usr/local/etc/rc.d/tn-vmtools.sh" || die "cannot install tn-vmtools.sh"

# ── 4. the template's config.xml ──────────────────────────────────────
cp "$CD/tn-admin.hash" "$T/var/tmp/tn-admin.hash" || die "no tn-admin.hash on the CD"
run /usr/local/bin/php -q /usr/local/sbin/tn-pfsense-config template /var/tmp/tn-admin.hash \
  || { rm -f "$T/var/tmp/tn-admin.hash"; die "tn-pfsense-config template"; }
rm -f "$T/var/tmp/tn-admin.hash"

# ── 5. marker, power off ──────────────────────────────────────────────
{
  echo "finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "pfsense=$VERSION"
  # The boot hook of this template applies and acknowledges a range admin hash (issue #248):
  # the worker waits for that acknowledgement only from a template that says so.
  echo "admin_ack=1"
  echo "packages=$ORDER"
} > "$T/cf/conf/tn-template-finish.txt" || die "cannot write the marker"
[ "$DEVFS" = 1 ] && umount "$T/dev"
sync
log "OK: template finished; powering off"
poweroff
