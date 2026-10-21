#!/usr/bin/env bash
# TrueNorth Range - refuse a Linux template the worker cannot configure. Runs just before
# cleanup.sh on every Linux build (bases and the clones derived from them); any failure
# fails the build, so no template is made. What it requires is what a clone needs on first
# boot (vsphere_api.py _customize_sync writes cloud-init guestinfo, nothing else):
#   * cloud-init, with its VMware datasource, first in datasource_list, and not disabled
#   * VMware Tools (open-vm-tools): the datasource reads guestinfo through it
#   * tn-ssh-hostkeys.service enabled: cleanup.sh deletes the host keys
set -uo pipefail
ROOT="${TN_TEST_ROOT:-}"   # test seam only (tests/contracts/test_template_build_safety.py)
fail() { echo "TEMPLATE NOT USABLE: $*" >&2; exit 1; }

command -v cloud-init >/dev/null 2>&1 \
  || fail "cloud-init is not installed; the worker configures Linux VMs only through cloud-init guestinfo"
echo "cloud-init $(cloud-init --version 2>&1)"
python3 -c 'import cloudinit.sources.DataSourceVMware' 2>/dev/null \
  || fail "this cloud-init has no VMware datasource (DataSourceVMware)"
grep -Eq '^datasource_list: *\[ *VMware' "$ROOT/etc/cloud/cloud.cfg.d/99-vmware.cfg" 2>/dev/null \
  || fail "/etc/cloud/cloud.cfg.d/99-vmware.cfg does not put VMware first"
[ ! -e "$ROOT/etc/cloud/cloud-init.disabled" ] || fail "cloud-init is disabled (/etc/cloud/cloud-init.disabled)"
enabled=0
for unit in cloud-init-local.service cloud-init.service cloud-init-main.service; do
  state="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
  case "$state" in enabled|static|enabled-runtime|alias) enabled=1 ;; esac
done
[ "$enabled" = 1 ] || fail "no cloud-init service is enabled"
command -v vmtoolsd >/dev/null 2>&1 || fail "open-vm-tools (vmtoolsd) is not installed"
[ "$(systemctl is-enabled tn-ssh-hostkeys.service 2>/dev/null || true)" = enabled ] \
  || fail "tn-ssh-hostkeys.service is not enabled: clones would have no SSH host keys"
echo "template OK: cloud-init (VMware datasource), open-vm-tools, SSH host-key regeneration"
