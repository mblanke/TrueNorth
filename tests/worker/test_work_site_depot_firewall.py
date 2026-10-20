"""Range firewalls at the work site reach TN-DEPOT01 on 10.30.32.10, ports 8081/3142 only (#214).

The work install's own values (install/inventory/group_vars/all/main.yml: tn_depot_url,
tn_depot_choco_feed, tn_depot_apt_proxy, tn_depot_ports and the range uplink pool/gateway),
with the range uplink network set to the port group the work-site kit puts the depot's
third NIC on (install/lab/work-site tn_depot_range_portgroup), go through the real
provisioner against the vSphere fakes of test_vsphere_provision.py. What the edge
firewall gets must be: a TN_DEPOT alias that is exactly the depot's range address, the
ports 8081 and 3142 and no others, and nothing else allowed out of the WAN.

Evidence kind: rendered configs against fakes. Nothing here proves that a real pfSense or
VyOS reaches a real depot; that is checked on site (install/lab/README.md).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import test_vsphere_provision as _vsp
import yaml
from test_appliance_configs import _build
from test_vsphere_provision import R8, RANGE_ID, SW_TEMPLATE, _pfsense_guestinfo, _prov, _rendered, _run, mod
from worker import vyos_config as vy

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = yaml.safe_load((ROOT / "install/inventory/group_vars/all/main.yml").read_text())
KIT = yaml.safe_load((ROOT / "install/lab/work-site/inventory/group_vars/all/main.yml").read_text())
DEPOT_IP = KIT["tn_depot_range_ip"]

# The fake vCenter, and the worker's part: reserve what allocation_needs declares (VLANs, and
# the edge's WAN address out of the installer's pool). worker_reserves is autouse.
vc, worker_reserves = _vsp.vc, _vsp.worker_reserves


@pytest.fixture
def work_site(vc, monkeypatch):
    """The provisioner configured as the work install would be once the uplink is switched on."""
    for name, value in {
        "VSPHERE_RANGE_UPLINK_NETWORK": KIT["tn_depot_range_portgroup"],
        "VSPHERE_RANGE_UPLINK_POOL": INSTALLER["tn_vsphere_range_uplink_pool"],
        "VSPHERE_RANGE_UPLINK_GATEWAY": str(INSTALLER["tn_vsphere_range_uplink_gateway"]),
        "VSPHERE_RANGE_UPLINK_PREFIX": int(INSTALLER["tn_vsphere_range_uplink_prefix"]),
        # vsphere_api reads TN_DEPOT_URL with .rstrip("/") at import.
        "TN_DEPOT_URL": INSTALLER["tn_depot_url"].rstrip("/"),
        "TN_DEPOT_CHOCO_FEED": INSTALLER["tn_depot_choco_feed"],
        "TN_DEPOT_APT_PROXY": INSTALLER["tn_depot_apt_proxy"],
        "TN_DEPOT_PORTS": INSTALLER["tn_depot_ports"],
    }.items():
        monkeypatch.setattr(mod, name, value)
    return vc


def test_the_kit_and_the_installer_name_the_same_depot():
    assert DEPOT_IP == "10.30.32.10"
    assert INSTALLER["tn_depot_url"] == f"http://{DEPOT_IP}"
    assert INSTALLER["tn_depot_ports"] == "8081,3142"


class TestPfsenseEdge:
    def test_depot_alias_is_the_range_nic_with_exactly_the_depot_ports(self, work_site):
        tpl = {**_vsp.TEMPLATE, "network": {**_vsp.TEMPLATE["network"], "firewall_rules": [
            {"name": "attacker-to-victim", "src": "attacker_infra", "dst": "victim_network", "action": "allow"},
        ]}}
        built = {**_rendered(tpl), "network": tpl["network"]}
        result = _run(_prov(work_site).provision(RANGE_ID, built, {}))
        assert result.status == "ok", result.errors
        fw = next(v for v in work_site.vms.values() if v.name == f"{R8}-fw")
        root, _ = _pfsense_guestinfo(fw)

        aliases = {a.findtext("name"): (a.findtext("type"), a.findtext("address")) for a in root.find("aliases")}
        assert aliases["TN_DEPOT"] == ("host", DEPOT_IP)
        assert aliases["TN_DEPOT_PORTS"] == ("port", "8081 3142")

        # The WAN is on the range uplink network, inside the installer's pool, with its gateway.
        wan = root.find("interfaces/wan")
        assert wan.findtext("ipaddr").startswith("10.30.32.") and 100 <= int(wan.findtext("ipaddr").rsplit(".", 1)[1]) <= 199
        assert root.findtext("gateways/gateway_item/gateway") == "10.30.32.1"

        rules = list(root.find("filter"))
        floating = [r for r in rules if r.findtext("floating") == "yes"]
        assert [(r.findtext("type"), r.findtext("interface"), r.findtext("direction")) for r in floating] == [
            ("pass", "wan", "out"), ("block", "wan", "out")]
        assert floating[0].findtext("destination/address") == "TN_DEPOT"
        assert floating[0].findtext("destination/port") == "TN_DEPOT_PORTS"
        assert floating[0].findtext("protocol") == "tcp"
        assert floating[1].find("destination/any") is not None  # everything else out of the WAN

        # Zone rules that reach the depot use the alias and the port alias, nothing wider;
        # no pass rule anywhere targets "any" (the internet, or the management network).
        to_depot = [r for r in rules if r.findtext("destination/address") == "TN_DEPOT"]
        assert to_depot and all(r.findtext("destination/port") == "TN_DEPOT_PORTS" for r in to_depot)
        assert all(r.findtext("protocol") == "tcp" for r in to_depot)
        for r in rules:
            if r.findtext("type") == "pass" and r.findtext("floating") != "yes":
                assert r.find("destination/any") is None, r.findtext("descr")
        xml = ET.tostring(root, encoding="unicode")
        assert "10.30.30." not in xml and "10.30.31." not in xml  # no MGMT or BUILD address anywhere


class TestVyosEdge:
    def test_depot_group_is_the_range_nic_with_exactly_the_depot_ports(self, work_site):
        result, extra, _ = _build(work_site, "vyos", {})
        assert result.status == "ok", result.errors
        cmds = vy.commands_of(extra)
        depot_addrs = [c for c in cmds if c.startswith("set firewall group address-group TN_DEPOT ")]
        assert depot_addrs == [f"set firewall group address-group TN_DEPOT address '{DEPOT_IP}'"]
        ports = {c.rsplit(" ", 1)[1].strip("'") for c in cmds
                 if c.startswith("set firewall group port-group TN_DEPOT_PORTS port ")}
        assert ports == {"8081", "3142"}
        fwd = "set firewall ipv4 forward filter"
        assert f"{fwd} default-action 'drop'" in cmds
        assert f"{fwd} rule 10 destination group address-group 'TN_DEPOT'" in cmds
        assert f"{fwd} rule 10 destination group port-group 'TN_DEPOT_PORTS'" in cmds
        assert f"{fwd} rule 10 protocol 'tcp'" in cmds
        assert "set protocols static route 0.0.0.0/0 next-hop '10.30.32.1'" in cmds
        assert not any("10.30.30." in c or "10.30.31." in c for c in cmds)


class TestSoftwareThroughTheRangeNic:
    def test_guests_use_the_feed_and_proxy_on_the_range_address(self, work_site):
        result = _run(_prov(work_site).provision(RANGE_ID, _rendered(SW_TEMPLATE), {}))
        assert result.status == "ok", result.errors
        started = work_site.guest_ops.started
        win = [s[3] for s in started if s[0] == f"{R8}-dc01" and s[2].endswith("choco.exe")]
        assert win and all(f'--source "http://{DEPOT_IP}:8081/repository/chocolatey/"' in a for a in win)
        lin = [s[3] for s in started if s[0] == f"{R8}-web01" and "apt-get" in s[3]]
        assert lin and all(f"Acquire::http::Proxy=http://{DEPOT_IP}:3142" in a for a in lin)

