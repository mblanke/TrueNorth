"""Range NIC MACs and guest-IP readiness settings reach the worker, per install (#232 review).

Each TrueNorth install on a shared vCenter needs its own VSPHERE_MAC_POOL slice: two installs
on one slice reserve MACs in separate databases and hand out the same ones. The installer
has a default and per-install values (work: hosts.yml, staging: staging.yml); the env
template and compose pass them through to the worker.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane/worker"))

from worker.provisioners import vsphere_macs  # noqa: E402

SETTINGS = {"VSPHERE_GUEST_IP_TIMEOUT": "tn_vsphere_guest_ip_timeout",
            "VSPHERE_GUEST_IP_CHECK": "tn_vsphere_guest_ip_check",
            "VSPHERE_MAC_POOL": "tn_vsphere_mac_pool",
            "VSPHERE_CUSTOMIZATION_GRACE": "tn_vsphere_customization_grace",  # #255
            "VSPHERE_CUSTOMIZATION_SKEW": "tn_vsphere_customization_skew"}


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
        assert f"{name}={{{{ {var} }}}}" in env, name
        assert f"{name}: ${{{name}:-" in compose, name
    assert defaults["tn_vsphere_guest_ip_timeout"] == 600 and defaults["tn_vsphere_guest_ip_check"] == "require"
    assert (defaults["tn_vsphere_customization_grace"], defaults["tn_vsphere_customization_skew"]) == (900, 300)
    assert "VSPHERE_CUSTOMIZATION_GRACE:-900}" in compose and "VSPHERE_CUSTOMIZATION_SKEW:-300}" in compose


def test_each_install_has_its_own_mac_slice_inside_vmwares_manual_range():
    work = vsphere_macs.parse_pool(_host_vars("hosts.yml")["tn_vsphere_mac_pool"])
    staging = vsphere_macs.parse_pool(_host_vars("staging.yml")["tn_vsphere_mac_pool"])
    assert work.last < staging.first or staging.last < work.first  # no MAC in both
