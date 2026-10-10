"""VyOS and OPNsense edge routers get the range's rendered config (vsphere_api TODO(appliance)).

Until now only pfSense was configured per range; VyOS and OPNsense booted with their
template config. Now:
- VyOS: worker/vyos_config.py renders configuration commands (interfaces pinned by MAC,
  default route, source NAT, a default-drop forward filter with depot-only egress and the
  template's rules) and delivers them as cloud-init user data (guestinfo.userdata,
  vyos_config_commands) before first power-on;
- OPNsense: pfsense_config.build_config(product="opnsense") renders the config.xml in
  OPNsense's layout, delivered in guestinfo.tn.opnsense.*, applied by the same boot
  script installed as tn-opnsense-config.
Tested against the pure renderers, the vSphere fakes of test_vsphere_provision.py and,
when PHP is available, the boot script.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest
import test_vsphere_provision as _vsp
import yaml
from test_pfsense_config import GUEST_MACS, PHP, SCRIPT, TEMPLATE_CONFIG, _firewall, _range
from test_vsphere_provision import MGMT, R8, RANGE_ID, TEMPLATE, FakeVM, FreshHardware, _prov, _run, _vm_out
from worker import pfsense_config as pf
from worker import render
from worker import vyos_config as vy
from worker.provisioners import vsphere_infra as infra

# The vSphere fakes' fixtures (a vCenter, its uplink and depot, the worker's reservations).
vc, uplink, depot, worker_reserves = _vsp.vc, _vsp.uplink, _vsp.depot, _vsp.worker_reserves


def _soc_router(os_name: str, wan: bool = True) -> tuple[dict, dict]:
    rng = _range("soc-training")
    vm = _firewall(rng, uplink=wan)
    return {**vm, "os": os_name, "template_name": f"tmpl-{os_name}"}, rng


# --------------------------------------------------------------------------- #
# VyOS renderer
# --------------------------------------------------------------------------- #
class TestVyosConfig:
    def _cfg(self, wan=True, depot_host="10.30.32.10", rules=None):
        vm, rng = _soc_router("vyos", wan)
        rules = rng["template"]["network"]["firewall_rules"] if rules is None else rules
        return vy.build_config(vm, networks=rng["networks"], rules=rules, depot_host=depot_host, hostname="fw01"), vm

    def test_interfaces_route_and_nat_follow_the_uplink(self):
        cfg, vm = self._cfg()
        cmds = cfg.commands
        assert "set system host-name 'fw01'" in cmds
        assert "set interfaces ethernet eth0 address '10.30.32.101/24'" in cmds
        assert "set interfaces ethernet eth0 description 'WAN'" in cmds
        zone_nics = [n for n in vm["nics"] if not n.get("uplink")]
        for i, nic in enumerate(zone_nics, start=1):
            assert f"set interfaces ethernet eth{i} address '{nic['ip']}/{nic.get('prefix', 24)}'" in cmds
        assert "set protocols static route 0.0.0.0/0 next-hop '10.30.32.1'" in cmds
        assert "set nat source rule 100 translation address 'masquerade'" in cmds
        assert "set firewall ipv4 input filter rule 2 inbound-interface name 'eth0'" in cmds

    def test_forward_filter_is_default_drop_with_depot_only_egress(self):
        cmds = self._cfg()[0].commands
        assert "set firewall ipv4 forward filter default-action 'drop'" in cmds
        assert "set firewall group address-group TN_DEPOT address '10.30.32.10'" in cmds
        assert {"set firewall group port-group TN_DEPOT_PORTS port '8081'",
                "set firewall group port-group TN_DEPOT_PORTS port '3142'"} <= set(cmds)
        assert "set firewall ipv4 forward filter rule 10 destination group address-group 'TN_DEPOT'" in cmds
        # nothing ever names the internet: every destination is a range zone or the depot
        dests = [c for c in cmds if " destination " in c and "forward filter" in c]
        assert all("TN_ZONES" in c or "TN_DEPOT" in c or "address '10.60." in c or " port " in c for c in dests)

    def test_template_rules_are_translated_and_untranslatable_ones_noted(self):
        rules = [{"name": "a2v", "src": "attacker_infra", "dst": "victim_network", "action": "allow", "ports": [80, 443]},
                 {"name": "span", "src": "*", "dst": "network_monitoring", "action": "mirror"},
                 {"name": "ghost", "src": "attacker_infra", "dst": "nowhere", "action": "allow"},
                 {"name": "odd", "src": "*", "dst": "*", "action": "teleport"}]
        vm, rng = _soc_router("vyos")
        rng["networks"] = rng["networks"] + [{"name": "attacker_infra", "cidr": "10.60.200.0/24"},
                                             {"name": "victim_network", "cidr": "10.60.201.0/24"}]
        cfg = vy.build_config(vm, networks=rng["networks"], rules=rules, depot_host="10.30.32.10")
        r = "set firewall ipv4 forward filter rule 100"
        assert f"{r} action 'accept'" in cfg.commands
        assert f"{r} destination port '80,443'" in cfg.commands and f"{r} protocol 'tcp_udp'" in cfg.commands
        assert not any("span" in c for c in cfg.commands)
        assert any("ghost" in n and "unknown destination zone" in n for n in cfg.notes)
        assert any("teleport" in n for n in cfg.notes)

    def test_isolated_range_has_no_wan_nat_or_depot(self):
        cfg, _ = self._cfg(wan=False)
        assert not any("nat source" in c or "TN_DEPOT" in c or "next-hop" in c for c in cfg.commands)
        assert "set interfaces ethernet eth0 description 'WAN'" not in cfg.commands
        assert any("every zone to every zone" in c for c in self._cfg(wan=False, rules=[])[0].commands)

    def test_a_hostname_depot_is_noted_not_guessed(self):
        cfg, _ = self._cfg(depot_host="depot.lab")
        assert not any("TN_DEPOT" in c for c in cfg.commands)
        assert any("not an IP address" in n for n in cfg.notes)

    def test_guestinfo_is_cloud_init_userdata_with_mac_pins(self):
        cfg, vm = self._cfg()
        macs = [f"00:50:56:AA:00:{i:02X}" for i in range(len(vm["nics"]))]
        values = vy.guestinfo(cfg, macs, "abcdef12-fw01")
        assert values["guestinfo.userdata.encoding"] == values["guestinfo.metadata.encoding"] == "base64"
        userdata = base64.b64decode(values["guestinfo.userdata"]).decode()
        assert userdata.startswith("#cloud-config\n")
        cmds = vy.commands_of(values)
        assert cmds[: len(macs)] == [f"set interfaces ethernet eth{i} hw-id '{m.lower()}'" for i, m in enumerate(macs)]
        assert cmds[len(macs):] == cfg.commands
        meta = yaml.safe_load(base64.b64decode(values["guestinfo.metadata"]))
        assert meta == {"instance-id": f"abcdef12-fw01-{cfg.sha256[:12]}", "local-hostname": "fw01"}
        with pytest.raises(vy.VyosConfigError, match="NICs"):
            vy.guestinfo(cfg, macs[:-1], "x")

    def test_no_value_can_break_out_of_its_quotes(self):
        vm, rng = _soc_router("vyos")
        cfg = vy.build_config(vm, networks=rng["networks"], rules=[], hostname="fw'; rm -rf / #")
        assert "set system host-name 'fw rm -rf / '" in cfg.commands


# --------------------------------------------------------------------------- #
# OPNsense renderer
# --------------------------------------------------------------------------- #
class TestOpnsenseConfig:
    def _cfg(self):
        vm, rng = _soc_router("opnsense")
        return pf.build_config(vm, networks=rng["networks"], rules=rng["template"]["network"]["firewall_rules"],
                               depot_host="10.30.32.10", hostname="fw01", product="opnsense"), vm

    def test_opnsense_layout(self):
        cfg, _ = self._cfg()
        root = ET.fromstring(cfg.xml)
        assert root.tag == "opnsense" and cfg.product == "opnsense"
        assert root.find("system/earlyshellcmd") is None  # the boot hook lives in rc.syshook.d
        assert {el.findtext("enable") for el in root.find("interfaces")} == {"1"}
        assert root.findtext("unbound/enable") == "1"
        assert root.findtext("interfaces/wan/ipaddr") == "10.30.32.101"
        assert root.findtext("nat/outbound/mode") == "automatic"
        assert [r.findtext("log") for r in root.find("filter") if r.find("log") is not None] == ["1"]

    def test_the_pfsense_config_is_unchanged(self):
        vm, rng = _soc_router("pfsense")
        root = ET.fromstring(pf.build_config(vm, networks=rng["networks"], hostname="fw01").xml)
        assert root.tag == "pfsense" and root.findtext("system/earlyshellcmd") == pf.BOOT_COMMAND
        assert not any(el.findtext("enable") for el in root.find("interfaces"))  # empty elements, as before

    def test_guestinfo_keys_are_opnsense_ones(self):
        cfg, vm = self._cfg()
        values = pf.guestinfo(cfg, [f"00:50:56:aa:00:{i:02x}" for i in range(len(vm["nics"]))])
        assert set(values) == {"guestinfo.tn.opnsense.config", "guestinfo.tn.opnsense.ifmap"}
        assert ET.fromstring(pf.decode(values["guestinfo.tn.opnsense.config"])).tag == "opnsense"

    def test_product_detection(self):
        assert pf.product_of({"os": "opnsense"}) == "opnsense"
        assert pf.product_of({"os": "x", "template_name": "tmpl-pfsense"}) == "pfsense"
        assert pf.product_of({"os": "vyos"}) is None and vy.is_vyos({"os": "vyos"})


# --------------------------------------------------------------------------- #
# Delivered by the vSphere provisioner (fakes)
# --------------------------------------------------------------------------- #
def _with_templates(vc, *names):
    for name in names:
        tmpl = FakeVM(name, [], template=True)
        tmpl.config.hardware = FreshHardware(MGMT)
        vc.templates[name] = tmpl
        vc.dc.vmFolder.childEntity.append(tmpl)
    return vc


def _build(vc, os_name: str, allocations: dict):
    tpl = {**TEMPLATE, "nodes": [{**TEMPLATE["nodes"][0], "os": os_name}, *TEMPLATE["nodes"][1:]]}
    images = {os_name: f"tmpl-{os_name}", "ubuntu-2404": "tmpl-ubuntu-2404", "windows-server-2022": "tmpl-win2022"}
    out = render.render_topology(tpl, RANGE_ID, images.get)
    built = {"name": "lab", "vms": out["vm_definitions"], "networks": out["network_definitions"],
             "network": tpl["network"]}
    result = _run(_prov(_with_templates(vc, f"tmpl-{os_name}")).provision(RANGE_ID, built, allocations))
    assert result.status == "ok", result.errors
    fw = next(v for v in vc.vms.values() if v.name == f"{R8}-fw")
    extra = {o.key: o.value for spec in fw.reconfig_specs for o in (spec.extraConfig or [])}
    macs = [c.macAddress.lower() for c in infra.nic_cards(fw.config.hardware.device)]
    return result, extra, macs


class TestDelivery:
    def test_vyos_edge_gets_its_commands_in_cloud_init_guestinfo(self, depot):
        result, extra, macs = _build(depot, "vyos", {"uplink_ip": "10.30.32.101"})
        cmds = vy.commands_of(extra)
        assert cmds[:4] == [f"set interfaces ethernet eth{i} hw-id '{m}'" for i, m in enumerate(macs)]
        assert "set interfaces ethernet eth0 address '10.30.32.101/24'" in cmds
        assert "set interfaces ethernet eth1 address '10.60.200.1/24'" in cmds
        assert "set firewall group address-group TN_DEPOT address '10.30.32.10'" in cmds
        assert not any(k.startswith("guestinfo.tn.") for k in extra)
        out = _vm_out(result, "fw")["vyos"]
        assert out["delivery"] == "cloud-init guestinfo" and len(out["config_sha256"]) == 64
        assert [i["if"] for i in out["interfaces"]] == ["eth0", "eth1", "eth2", "eth3"]

    def test_opnsense_edge_gets_its_config_xml_in_guestinfo(self, depot):
        result, extra, macs = _build(depot, "opnsense", {"uplink_ip": "10.30.32.101"})
        root = ET.fromstring(pf.decode(extra["guestinfo.tn.opnsense.config"]))
        assert root.tag == "opnsense" and root.findtext("interfaces/wan/ipaddr") == "10.30.32.101"
        assert extra["guestinfo.tn.opnsense.ifmap"] == " ".join(f"vmx{i}={m}" for i, m in enumerate(macs))
        assert "guestinfo.tn.pfsense.config" not in extra
        assert _vm_out(result, "fw")["opnsense"]["delivery"] == "guestinfo"

    def test_isolated_vyos_router_still_gets_its_zones(self, vc):
        _, extra, _ = _build(vc, "vyos", {})
        cmds = vy.commands_of(extra)
        assert "set interfaces ethernet eth0 address '10.60.200.1/24'" in cmds
        assert not any("nat source" in c for c in cmds)


# --------------------------------------------------------------------------- #
# The boot script as tn-opnsense-config (PHP; skipped without a PHP CLI)
# --------------------------------------------------------------------------- #
def _run_as_opnsense(tmp_path, root, *args):
    """Run the boot script under the name the OPNsense template installs it as."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "tn-opnsense-config"
    shutil.copy(SCRIPT, script)
    if shutil.which("php"):
        cmd = ["php", str(script), *args]
    else:
        cmd = ["docker", "run", "--rm", "-e", f"TN_PFSENSE_TEST_ROOT={root}", "-v", f"{root}:{root}",
               "-v", f"{bindir}:/s:ro", "php:8.2-cli", "php", "/s/tn-opnsense-config", *args]
    return subprocess.run(cmd, env={**os.environ, "TN_PFSENSE_TEST_ROOT": str(root)}, capture_output=True,
                          text=True, timeout=300)


@pytest.mark.skipif(PHP is None, reason="no php CLI (install php, or TN_PHP_DOCKER=1 with docker)")
class TestOpnsenseBootScript:
    def test_applies_an_opnsense_config_and_keeps_the_template_users(self, tmp_path):
        root = tmp_path / "fw"
        (root / "conf").mkdir(parents=True)
        (root / "guestinfo").mkdir()
        template = ET.parse(TEMPLATE_CONFIG).getroot()
        template.tag = "opnsense"
        ET.ElementTree(template).write(root / "conf" / "config.xml")
        lines = []
        for name, mac in GUEST_MACS.items():
            lines += [f"{name}: flags=8843<UP,BROADCAST,RUNNING> metric 0 mtu 1500", f"\tether {mac}"]
        (root / "ifconfig.txt").write_text("\n".join(lines) + "\n")
        vm, rng = _soc_router("opnsense")
        cfg = pf.build_config(vm, networks=rng["networks"], hostname="fw01", product="opnsense")
        for key, value in pf.guestinfo(cfg, [f"00:50:56:aa:00:{i:02x}" for i in range(len(vm["nics"]))]).items():
            (root / "guestinfo" / key).write_text(value)
        res = _run_as_opnsense(tmp_path, root, "boot")
        assert res.returncode == 0, res.stdout + res.stderr
        assert "would reboot" in res.stdout and "tn-opnsense-config:" in res.stdout
        applied = ET.parse(root / "conf" / "config.xml").getroot()
        assert applied.tag == "opnsense" and applied.findtext("system/user/name") == "admin"
        assert applied.findtext("interfaces/wan/ipaddr") == "10.30.32.101"
        assert (root / "conf" / "tn-opnsense-config.log").exists()
        shutil.rmtree(root)
