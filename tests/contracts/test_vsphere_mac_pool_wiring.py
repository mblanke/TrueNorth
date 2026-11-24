# Author: Marc-Andre Blanke
# Created: 2026-10-20T18:59:33Z
"""Range NIC MACs and guest-IP readiness settings reach the worker, per install (#232 review).

Each TrueNorth install on a shared vCenter needs its own VSPHERE_MAC_POOL slice: two installs
on one slice reserve MACs in separate databases and hand out the same ones. The installer
has a default and per-install values (work: hosts.yml, staging: staging.yml); the env
template and compose pass them through to the worker.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane/worker"))

from worker.provisioners import vsphere_infra, vsphere_macs  # noqa: E402

SETTINGS = {"VSPHERE_GUEST_IP_TIMEOUT": "tn_vsphere_guest_ip_timeout",
            "VSPHERE_GUEST_IP_CHECK": "tn_vsphere_guest_ip_check",
            "VSPHERE_MAC_POOL": "tn_vsphere_mac_pool",
            "VSPHERE_CUSTOMIZATION_GRACE": "tn_vsphere_customization_grace",  # #255
            "VSPHERE_CUSTOMIZATION_SKEW": "tn_vsphere_customization_skew",
            "VSPHERE_WINDOWS_TIMEZONE": "tn_vsphere_windows_timezone"}  # #526


def _host_vars(inventory: str) -> dict:
    doc = yaml.safe_load((ROOT / "install/inventory" / inventory).read_text())
    (host,) = doc["all"]["children"]["platform"]["hosts"].values()
    return host


def test_the_installer_defaults_and_env_template_carry_every_setting():
    defaults = yaml.safe_load((ROOT / "install/inventory/group_vars/all/main.yml").read_text())
    env = (ROOT / "install/roles/tn_config/templates/env.production.j2").read_text()
    compose = (ROOT / "infra/platform/docker/compose.prod.yml").read_text()
    for name, var in SETTINGS.items():
        assert var in defaults, var
        assert re.search(rf"^{name}={{{{ {var}( \| default\('', true\))? }}}}$", env, re.M), name
        assert f"{name}: ${{{name}:-" in compose, name
    assert defaults["tn_vsphere_guest_ip_timeout"] == 600 and defaults["tn_vsphere_guest_ip_check"] == "require"
    assert (defaults["tn_vsphere_customization_grace"], defaults["tn_vsphere_customization_skew"]) == (900, 300)
    assert "VSPHERE_CUSTOMIZATION_GRACE:-900}" in compose and "VSPHERE_CUSTOMIZATION_SKEW:-300}" in compose
    # #526: Windows range VMs get Eastern Standard Time (35) unless a site sets another index.
    assert defaults["tn_vsphere_windows_timezone"] == 35 and "VSPHERE_WINDOWS_TIMEZONE:-35}" in compose


def test_preflight_and_test_clone_accept_exactly_the_workers_windows_time_zones():
    """#526: VSPHERE_WINDOWS_TIMEZONE is checked three times (installer preflight, the
    worker, scripts/lab/test-clone.sh --tz), against one index set."""
    want = set(vsphere_infra.WINDOWS_TIMEZONE_INDEXES)
    assert {0, 35, 85} <= want and not {53, 133} & want and vsphere_infra.WINDOWS_TIMEZONE_DEFAULT == 35  # 53/133: govc's hex reading of 035/85
    tasks = yaml.safe_load((ROOT / "install/roles/tn_preflight/tasks/main.yml").read_text())
    (task,) = [t for t in tasks if "tn_vsphere_windows_timezone" in t.get("name", "")]
    assert set(task["vars"]["tn_windows_timezone_indexes"]) == want
    assert "default('', true)" in task["vars"]["tn_windows_timezone_raw"]
    script = (ROOT / "scripts/lab/test-clone.sh").read_text()
    (line,) = [ln for ln in script.splitlines() if ln.startswith("WIN_TZ_INDEXES=")]
    assert set(map(int, line.split("=", 1)[1].strip('"').split())) == want
    # The script's own check, run on its own (die stubbed): accepts only those indexes.
    (check,) = [ln for ln in script.splitlines() if ln.startswith('[[ -z "$WIN_TZ" ]] ||')]
    for tz, ok in (("85", True), ("035", True), ("0", True), ("", True), ("133", False), ("53", False),
                   ("0x55", False), ("850", False)):
        r = subprocess.run(["bash", "-c", f'set -euo pipefail; die() {{ exit 3; }}; {line}; WIN_TZ="{tz}"; {check}'],
                           capture_output=True, text=True)
        assert r.returncode == (0 if ok else 3), (tz, r.stderr)


def test_each_install_has_its_own_mac_slice_inside_vmwares_manual_range():
    work = vsphere_macs.parse_pool(_host_vars("hosts.yml")["tn_vsphere_mac_pool"])
    staging = vsphere_macs.parse_pool(_host_vars("staging.yml")["tn_vsphere_mac_pool"])
    assert work.last < staging.first or staging.last < work.first  # no MAC in both
