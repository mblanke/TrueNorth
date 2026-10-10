"""The per-range pfSense config.xml (worker/pfsense_config.py) and its boot script.

The generator is pure, so these tests parse the XML it produces. The boot script
(infra/vsphere/packer/files/pfsense/tn-pfsense-config) is PHP and runs inside pfSense;
the last class runs it against a fake firewall directory when a PHP CLI is available
(``php`` on PATH, or ``TN_PHP_DOCKER=1`` with docker: the php:8.2-cli image), else skips.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml
from worker import pfsense_config as pf
from worker import render

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra" / "vsphere" / "packer" / "files" / "pfsense" / "tn-pfsense-config"
TEMPLATE_CONFIG = REPO / "infra" / "vsphere" / "packer" / "http" / "pfsense" / "config.xml"
RANGE_ID = "abcdef12-3456-7890-abcd-ef1234567890"
UPLINK = {"uplink": True, "vlan": None, "network": "dPG-TN-SVC", "ip": "10.30.32.101", "prefix": 24,
          "netmask": "255.255.255.0", "gateway": "10.30.32.1"}


def _range(name: str) -> dict:
    template = yaml.safe_load((REPO / "content" / "ranges" / name / "template.yaml").read_text())
    out = render.render_topology(template, RANGE_ID, lambda alias: alias)
    return {"template": template, "vms": out["vm_definitions"], "networks": out["network_definitions"]}


def _firewall(rng: dict, node_id: str = "fw01", uplink: bool = True) -> dict:
    vm = next(v for v in rng["vms"] if v["node_id"] == node_id)
    vm = {**vm, "nics": [dict(n) for n in vm["nics"]]}
    if uplink:
        vm["nics"].insert(0, dict(UPLINK))
    return vm


def _rules(root: ET.Element) -> list[ET.Element]:
    return list(root.find("filter"))


def _rule(root: ET.Element, descr_prefix: str) -> list[ET.Element]:
    return [r for r in _rules(root) if (r.findtext("descr") or "").startswith(descr_prefix)]


def _soc(uplink: bool = True, depot: str = "10.30.32.10") -> tuple[pf.PfsenseConfig, ET.Element]:
    rng = _range("soc-training")
    cfg = pf.build_config(_firewall(rng, uplink=uplink), networks=rng["networks"],
                          rules=rng["template"]["network"]["firewall_rules"], depot_host=depot, hostname="fw01")
    return cfg, ET.fromstring(cfg.xml)


class TestSocTrainingEdge:
    def test_interfaces_follow_nic_order_wan_first(self):
        cfg, root = _soc()
        got = [(el.tag, el.findtext("if"), el.findtext("descr"), el.findtext("ipaddr"), el.findtext("subnet"))
               for el in root.find("interfaces")]
        assert got == [
            ("wan", "vmx0", "WAN", "10.30.32.101", "24"),
            ("lan", "vmx1", "attacker_infra", "10.60.200.1", "24"),
            ("opt1", "vmx2", "victim_network", "10.60.201.1", "24"),
            ("opt2", "vmx3", "soc_tools", "10.60.202.1", "24"),
            ("opt3", "vmx4", "network_monitoring", "10.60.203.1", "24"),
            ("opt4", "vmx5", "management", "10.60.204.1", "24"),
        ]
        assert root.findtext("interfaces/wan/gateway") == "WANGW"
        assert root.findtext("gateways/gateway_item/gateway") == "10.30.32.1"
        assert root.findtext("gateways/defaultgw4") == "WANGW"
        assert [i.key for i in cfg.interfaces] == ["wan", "lan", "opt1", "opt2", "opt3", "opt4"]

    def test_nat_resolver_and_boot_hook(self):
        _, root = _soc()
        assert root.findtext("nat/outbound/mode") == "automatic"
        assert root.find("unbound/enable") is not None
        assert root.findtext("unbound/active_interface") == "lo0,lan,opt1,opt2,opt3,opt4"
        assert root.findtext("system/earlyshellcmd") == pf.BOOT_COMMAND
        assert root.findtext("system/hostname") == "fw01"
        assert root.find("system/already_run_config_wizard") is not None
        assert list(root.find("dhcpd")) == []

    def test_no_credentials_in_the_config(self):
        cfg, root = _soc()
        assert root.find("system/user") is None and root.find("system/group") is None
        assert "bcrypt" not in cfg.xml and "password" not in cfg.xml.lower()

    def test_only_depot_traffic_leaves_through_the_wan(self):
        _, root = _soc()
        floating = [r for r in _rules(root) if r.findtext("floating") == "yes"]
        assert [(r.findtext("type"), r.findtext("interface"), r.findtext("direction"), r.findtext("quick"))
                for r in floating] == [("pass", "wan", "out", "yes"), ("block", "wan", "out", "yes")]
        assert floating[0].findtext("destination/address") == "TN_DEPOT"
        assert floating[0].findtext("destination/port") == "TN_DEPOT_PORTS"
        assert floating[0].findtext("protocol") == "tcp"
        assert floating[1].find("destination/any") is not None
        aliases = {a.findtext("name"): (a.findtext("type"), a.findtext("address")) for a in root.find("aliases")}
        assert aliases["TN_DEPOT"] == ("host", "10.30.32.10")
        assert aliases["TN_DEPOT_PORTS"] == ("port", "8081 3142")
        assert aliases["TN_ZONES"][1].split() == [f"10.60.{o}.0/24" for o in range(200, 205)]
        # Each zone may reach the depot ports, and nothing in the zone rules targets "any".
        depot = [r for r in _rules(root) if (r.findtext("descr") or "").endswith("software depot")]
        assert sorted(r.findtext("interface") for r in depot) == ["lan", "opt1", "opt2", "opt3", "opt4"]
        for r in _rules(root):
            if r.findtext("floating") != "yes" and r.findtext("type") == "pass":
                assert r.find("destination/any") is None, r.findtext("descr")

    def test_template_rules_become_interface_rules(self):
        cfg, root = _soc()
        (a2v,) = _rule(root, "attacker-to-victim")
        assert (a2v.findtext("type"), a2v.findtext("interface")) == ("pass", "lan")
        assert a2v.findtext("source/network") == "lan" and a2v.findtext("destination/network") == "opt1"
        assert a2v.find("protocol") is None  # any protocol
        (v2a,) = _rule(root, "victim-to-attacker")
        assert v2a.findtext("interface") == "opt1" and v2a.findtext("protocol") == "tcp/udp"
        port_alias = v2a.findtext("destination/port")
        alias = next(a for a in root.find("aliases") if a.findtext("name") == port_alias)
        assert alias.findtext("type") == "port" and alias.findtext("address") == "80 443 53 8080"
        assert [r.findtext("interface") for r in _rule(root, "soc-to")] == ["opt2", "opt2"]
        # SPAN is the monitoring port group's job; no pf rule and no warning for it.
        assert not _rule(root, "monitoring-mirror") and cfg.notes == []
        # With template rules there is no blanket zone-to-zone allow.
        assert not [r for r in _rules(root) if "to every range zone" in (r.findtext("descr") or "")]

    def test_trackers_are_unique(self):
        _, root = _soc()
        trackers = [r.findtext("tracker") for r in _rules(root)]
        assert len(trackers) == len(set(trackers))


class TestVariants:
    def test_without_template_rules_every_zone_reaches_every_zone(self):
        rng = _range("soc-training")
        cfg = pf.build_config(_firewall(rng), networks=rng["networks"], rules=[], depot_host="10.30.32.10")
        root = ET.fromstring(cfg.xml)
        allow = [r for r in _rules(root) if "to every range zone" in (r.findtext("descr") or "")]
        assert [(r.findtext("interface"), r.findtext("destination/address")) for r in allow] == [
            (k, "TN_ZONES") for k in ("lan", "opt1", "opt2", "opt3", "opt4")]

    def test_isolated_range_first_zone_takes_the_wan_slot(self):
        cfg, root = _soc(uplink=False)
        assert root.findtext("interfaces/wan/if") == "vmx0"
        assert root.findtext("interfaces/wan/ipaddr") == "10.60.200.1"
        assert root.findtext("interfaces/wan/descr") == "attacker_infra"
        assert root.find("interfaces/wan/gateway") is None and root.find("gateways") is None
        assert root.findtext("nat/outbound/mode") == "disabled"
        assert not [r for r in _rules(root) if r.findtext("floating")]
        assert not [a for a in root.find("aliases") if a.findtext("name") == "TN_DEPOT"]
        (a2v,) = _rule(root, "attacker-to-victim")
        assert a2v.findtext("interface") == "wan" and a2v.findtext("destination/network") == "lan"

    def test_uplink_without_a_depot_blocks_all_egress(self):
        _, root = _soc(depot="")
        floating = [r for r in _rules(root) if r.findtext("floating") == "yes"]
        assert [r.findtext("type") for r in floating] == ["block"]
        assert not [r for r in _rules(root) if (r.findtext("descr") or "").endswith("software depot")]

    def test_star_source_and_unknown_zones(self):
        rng = _range("soc-training")
        rules = [{"name": "all-to-mgmt", "src": "*", "dst": "management", "ports": [22], "action": "allow"},
                 {"name": "bad-src", "src": "mars", "dst": "management", "action": "allow"},
                 {"name": "nat-ish", "src": "management", "dst": "*", "action": "redirect"}]
        cfg = pf.build_config(_firewall(rng), networks=rng["networks"], rules=rules)
        root = ET.fromstring(cfg.xml)
        star = _rule(root, "all-to-mgmt")
        assert len(star) == 5 and {r.findtext("destination/port") for r in star} == {"22"}
        assert len(cfg.notes) == 2
        assert "unknown source zone 'mars'" in cfg.notes[0] and "'redirect'" in cfg.notes[1]

    def test_deny_rules_block(self):
        rng = _range("red-vs-blue")
        fw = _firewall(rng, "fw01")
        cfg = pf.build_config(fw, networks=rng["networks"], rules=rng["template"]["network"]["firewall_rules"])
        root = ET.fromstring(cfg.xml)
        zones = {i.network for i in cfg.interfaces}
        # Only rules whose source zone sits on this firewall; the others belong to fw02.
        for r in _rules(root):
            if r.findtext("floating"):
                continue
            key = r.findtext("interface")
            assert key in {i.key for i in cfg.interfaces}
        assert ("red_team" in zones) == bool(_rule(root, "red-to-corp-deny"))
        for r in _rule(root, "red-to-"):
            if "deny" in r.findtext("descr"):
                assert r.findtext("type") == "block"

    @pytest.mark.parametrize("name", sorted(p.name for p in (REPO / "content" / "ranges").iterdir()
                                            if (p / "template.yaml").exists()))
    def test_every_library_range_renders(self, name):
        rng = _range(name)
        rules = ((rng["template"].get("network") or {}).get("firewall_rules")) or []
        fws = [v for v in rng["vms"] if pf.is_pfsense(v)]
        for i, vm in enumerate(fws):
            vm = {**vm, "nics": ([dict(UPLINK)] if i == 0 else []) + [dict(n) for n in vm["nics"]]}
            cfg = pf.build_config(vm, networks=rng["networks"], rules=rules, depot_host="10.30.32.10")
            root = ET.fromstring(cfg.xml)
            assert root.tag == "pfsense"
            assert not [n for n in cfg.notes if "unknown" in n], cfg.notes  # the library's zones all exist
            pf.guestinfo(cfg, [f"00:50:56:00:00:{j:02x}" for j in range(len(cfg.interfaces))])


class TestGuestinfo:
    def test_round_trip_and_ifmap(self):
        cfg, _ = _soc()
        macs = [f"00:50:56:AA:00:{i:02X}" for i in range(6)]
        values = pf.guestinfo(cfg, macs)
        assert set(values) == {pf.GUESTINFO_CONFIG, pf.GUESTINFO_IFMAP}
        assert pf.decode(values[pf.GUESTINFO_CONFIG]) == cfg.xml
        assert values[pf.GUESTINFO_IFMAP] == " ".join(f"vmx{i}=00:50:56:aa:00:{i:02x}" for i in range(6))
        assert pf.guestinfo(cfg, macs) == values  # deterministic: same range, same bytes
        assert len(values[pf.GUESTINFO_CONFIG]) < 8000

    def test_nic_count_must_match(self):
        cfg, _ = _soc()
        with pytest.raises(pf.PfsenseConfigError, match="expects 6"):
            pf.guestinfo(cfg, ["00:50:56:00:00:01"])

    def test_zone_without_address_is_refused(self):
        vm = {"name": "fw", "nics": [{"vlan": 1, "network": "a", "ip": "", "prefix": 24}]}
        with pytest.raises(pf.PfsenseConfigError):
            pf.build_config(vm, networks=[])

    def test_boot_script_uses_the_same_keys_and_command(self):
        text = SCRIPT.read_text()
        # The script composes its keys and boot command from the product (pfsense | opnsense).
        assert "define('KEY_CONFIG', 'guestinfo.tn.' . PRODUCT . '.config');" in text
        assert "define('KEY_IFMAP', 'guestinfo.tn.' . PRODUCT . '.ifmap');" in text
        assert pf.GUESTINFO_CONFIG == "guestinfo.tn.pfsense.config" and pf.GUESTINFO_IFMAP == "guestinfo.tn.pfsense.ifmap"
        assert "define('TN_SCRIPT', '/usr/local/sbin/tn-' . PRODUCT . '-config');" in text
        assert "define('BOOT_COMMAND', '/usr/local/bin/php -q ' . TN_SCRIPT . ' boot');" in text
        assert pf.BOOT_COMMAND == "/usr/local/bin/php -q /usr/local/sbin/tn-pfsense-config boot"
        assert f"<earlyshellcmd>{pf.BOOT_COMMAND}</earlyshellcmd>" in TEMPLATE_CONFIG.read_text()
        assert os.access(SCRIPT, os.X_OK)


# --------------------------------------------------------------------------- #
# The boot script, run against a fake firewall directory
# --------------------------------------------------------------------------- #


def _php_runner():
    if shutil.which("php"):
        return lambda root, *args: subprocess.run(
            ["php", str(SCRIPT), *args], env={**os.environ, "TN_PFSENSE_TEST_ROOT": str(root)},
            capture_output=True, text=True, timeout=60)
    if os.getenv("TN_PHP_DOCKER") == "1" and shutil.which("docker"):
        return lambda root, *args: subprocess.run(
            ["docker", "run", "--rm", "-e", f"TN_PFSENSE_TEST_ROOT={root}", "-v", f"{root}:{root}",
             "-v", f"{SCRIPT.parent}:/s:ro", "php:8.2-cli", "php", "/s/tn-pfsense-config", *args],
            capture_output=True, text=True, timeout=300)
    return None


PHP = _php_runner()
# The guest names its NICs differently from vSphere's order: vmx4/vmx5 swapped.
GUEST_MACS = {"vmx0": "00:50:56:aa:00:00", "vmx1": "00:50:56:aa:00:01", "vmx2": "00:50:56:aa:00:02",
              "vmx3": "00:50:56:aa:00:03", "vmx4": "00:50:56:aa:00:05", "vmx5": "00:50:56:aa:00:04"}


@pytest.mark.skipif(PHP is None, reason="no php CLI (install php, or TN_PHP_DOCKER=1 with docker)")
class TestBootScript:
    @pytest.fixture
    def fw(self, tmp_path):
        root = tmp_path / "fw"
        (root / "conf").mkdir(parents=True)
        (root / "guestinfo").mkdir()
        shutil.copy(TEMPLATE_CONFIG, root / "conf" / "config.xml")
        lines = ["lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 16384"]
        for name, mac in GUEST_MACS.items():
            lines += [f"{name}: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> metric 0 mtu 1500",
                      "\toptions=4e403bb<RXCSUM,TXCSUM>", f"\tether {mac}", "\tmedia: Ethernet autoselect"]
        (root / "ifconfig.txt").write_text("\n".join(lines) + "\n")
        return root

    def _deliver(self, root: Path) -> pf.PfsenseConfig:
        cfg, _ = _soc()
        values = pf.guestinfo(cfg, [f"00:50:56:aa:00:{i:02x}" for i in range(6)])
        for key, value in values.items():
            (root / "guestinfo" / key).write_text(value)
        return cfg

    def test_no_guestinfo_leaves_the_template_alone(self, fw):
        before = (fw / "conf" / "config.xml").read_text()
        res = PHP(fw, "boot")
        assert res.returncode == 0, res.stdout + res.stderr
        assert (fw / "conf" / "config.xml").read_text() == before

    def test_applies_once_maps_nics_by_mac_and_keeps_the_template_users(self, fw):
        self._deliver(fw)
        template = ET.parse(TEMPLATE_CONFIG).getroot()
        res = PHP(fw, "boot")
        assert res.returncode == 0, res.stdout + res.stderr
        assert "would reboot" in res.stdout

        root = ET.parse(fw / "conf" / "config.xml").getroot()
        ifs = {el.tag: el.findtext("if") for el in root.find("interfaces")}
        # vSphere NIC 4 (network_monitoring) has the MAC the guest calls vmx5, and back.
        assert ifs == {"wan": "vmx0", "lan": "vmx1", "opt1": "vmx2", "opt2": "vmx3", "opt3": "vmx5", "opt4": "vmx4"}
        assert root.findtext("interfaces/opt3/ipaddr") == "10.60.203.1"
        assert root.findtext("system/user/name") == "admin"
        assert root.findtext("system/user/bcrypt-hash") == template.findtext("system/user/bcrypt-hash")
        assert root.findtext("system/group/name") == "admins"
        assert root.findtext("version") == template.findtext("version")
        assert root.findtext("system/earlyshellcmd") == pf.BOOT_COMMAND
        assert len(root.findall("version")) == 1 and len(root.findall("system/user")) == 1
        assert (fw / "conf" / "config.xml.tn-previous").exists()
        assert len((fw / "conf" / "tn-guestinfo.sha256").read_text().strip()) == 64

        # Same guestinfo on the next boot: nothing to do, no reboot loop.
        again = PHP(fw, "boot")
        assert again.returncode == 0 and "would reboot" not in again.stdout

    def test_bad_guestinfo_changes_nothing(self, fw):
        (fw / "guestinfo" / pf.GUESTINFO_CONFIG).write_text("not base64 at all!")
        before = (fw / "conf" / "config.xml").read_text()
        res = PHP(fw, "boot")
        assert res.returncode == 1 and "NOT applied" in res.stdout
        assert (fw / "conf" / "config.xml").read_text() == before

    def test_unknown_mac_changes_nothing(self, fw):
        self._deliver(fw)
        (fw / "guestinfo" / pf.GUESTINFO_IFMAP).write_text("vmx0=00:50:56:ff:ff:ff")
        before = (fw / "conf" / "config.xml").read_text()
        res = PHP(fw, "boot")
        assert res.returncode == 1 and "no NIC with MAC" in res.stdout
        assert (fw / "conf" / "config.xml").read_text() == before
