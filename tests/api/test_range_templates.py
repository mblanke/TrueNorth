"""Every content/ranges/*/template.yaml must be a valid address plan and a sane size.

medium-enterprise once shipped 10.10.300.0/24 and 10.10.400.x: the VLAN id had been used as
the third octet, so the CIDR did not parse and the renderer silently fell back to 10.0.0.0/24.
VLAN ids are logical labels (the provisioner maps them to physical VLANs); the subnet is an
independent address plan and has to be valid on its own.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = sorted((ROOT / "content" / "ranges").glob("*/template.yaml"))
IPV4ISH = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?$")

_spec = importlib.util.spec_from_file_location("range_capacity", ROOT / "scripts" / "range-capacity.py")
rc = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("range_capacity", rc)  # dataclasses look their module up
_spec.loader.exec_module(rc)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}


def _strings(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
    elif isinstance(obj, str):
        yield obj.strip()


def _vlans(tpl: dict) -> dict[str, ipaddress.IPv4Network]:
    return {v["name"]: ipaddress.ip_network(v["cidr"]) for v in (tpl.get("network") or {}).get("vlans") or []}


ids = [p.parent.name for p in TEMPLATES]


def test_every_range_template_is_found():
    assert len(TEMPLATES) >= 7, TEMPLATES


@pytest.mark.parametrize("path", TEMPLATES, ids=ids)
def test_every_address_like_string_parses(path):
    """Any dotted-quad anywhere in the template (CIDRs, IPs, inputs, interfaces) is valid."""
    bad = []
    for s in _strings(_load(path)):
        if IPV4ISH.match(s):
            try:
                ipaddress.ip_interface(s) if "/" in s else ipaddress.ip_address(s)
            except ValueError:
                bad.append(s)
    assert not bad, f"{path.parent.name}: invalid addresses {bad}"


@pytest.mark.parametrize("path", TEMPLATES, ids=ids)
def test_vlans_are_unique_strict_and_disjoint(path):
    vlans = (_load(path).get("network") or {}).get("vlans") or []
    names = [v["name"] for v in vlans]
    vids = [v["id"] for v in vlans]
    assert len(names) == len(set(names)), names
    assert len(vids) == len(set(vids)), vids
    nets = []
    for v in vlans:
        net = ipaddress.ip_network(v["cidr"])  # strict: host bits must be zero
        assert net.version == 4 and net.is_private, v
        nets.append(net)
    overlaps = [(a, b) for i, a in enumerate(nets) for b in nets[i + 1:] if a.overlaps(b)]
    assert not overlaps, overlaps


@pytest.mark.parametrize("path", TEMPLATES, ids=ids)
def test_node_ips_lie_inside_their_vlan(path):
    tpl = _load(path)
    vlans = _vlans(tpl)
    owner: dict[str, str] = {}
    for node in tpl.get("nodes") or []:
        nics = {(node["vlan"], node.get("ip"))}
        nics |= {(i["vlan"], i.get("ip")) for i in node.get("interfaces") or []}
        for vlan, ip in nics:
            assert vlan in vlans, f"{node['id']}: VLAN {vlan!r} is not defined"
            if not ip:
                continue
            addr = ipaddress.ip_address(ip)
            net = vlans[vlan]
            assert addr in net, f"{node['id']}: {ip} is not in {vlan} {net}"
            assert addr not in (net.network_address, net.broadcast_address), f"{node['id']}: {ip}"
            assert owner.setdefault(ip, node["id"]) == node["id"], f"{ip} used by {owner[ip]} and {node['id']}"


@pytest.mark.parametrize("path", TEMPLATES, ids=ids)
def test_firewall_rules_name_defined_vlans(path):
    tpl = _load(path)
    names = set(_vlans(tpl)) | {"*"}
    for rule in (tpl.get("network") or {}).get("firewall_rules") or []:
        assert rule["src"] in names and rule["dst"] in names, rule


@pytest.mark.parametrize("path", TEMPLATES, ids=ids)
def test_input_cidrs_match_the_vlans(path):
    tpl = _load(path)
    cidrs = {v for k, v in (tpl.get("inputs") or {}).items() if k.startswith("cidr_")}
    for c in cidrs:
        ipaddress.ip_network(c)
    if cidrs and tpl.get("nodes"):
        assert cidrs <= {str(n) for n in _vlans(tpl).values()}, cidrs


# ── sizes ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", TEMPLATES, ids=ids)
def test_declared_totals_match_the_nodes(path):
    tpl = _load(path)
    t = rc.range_totals(tpl)
    declared = tpl.get("resource_totals")
    if declared:
        assert (declared["total_vms"], declared["total_cores"], declared["total_memory_gb"],
                declared["total_disk_gb"]) == (t.vms, t.vcpu, round(t.ram_gb), t.disk_gb)
    totals = (tpl.get("deployment") or {}).get("totals")
    if totals:
        assert (totals["vms"], totals["vcpu"], totals["ram_gb"], totals["disk_gb"]) == (
            t.vms, t.vcpu, round(t.ram_gb), t.disk_gb)
    if "vm_count" in tpl:
        assert tpl["vm_count"] == t.vms


@pytest.mark.parametrize("path", TEMPLATES, ids=ids)
def test_role_minimums(path):
    """Right-sizing must not go below what the role needs (vm-build-sheet.md sections 2, 3, 7)."""
    for node in _load(path).get("nodes") or []:
        cores, mem, disk = rc._specs(node)
        services = {str(s).lower() for s in node.get("services") or []}
        if node["os"] == "windows-11":
            assert disk >= 64, f"{node['id']}: Windows 11 needs a 64 GB disk"
        if node["os"] == "securityonion":
            assert cores >= 4 and mem >= 16384, f"{node['id']}: Security Onion needs 4 vCPU / 16 GB"
        if any(s.startswith("exchange") for s in services):
            assert cores >= 4 and mem >= 16384 and disk >= 150, f"{node['id']}: Exchange needs 4 / 16 GB / 150 GB"
        if any(s.startswith("sharepoint") or s.startswith("mssql") for s in services):
            assert cores >= 4 and mem >= 16384, f"{node['id']}: SharePoint/SQL need 4 vCPU / 16 GB"
        if node["role"] == "siem":
            assert cores >= 4 and mem >= 16384, f"{node['id']}: SIEM needs 4 vCPU / 16 GB"
        if node["os"].startswith("windows-server"):
            assert disk >= 35, f"{node['id']}: below the 35 GB srv golden image"
        if node["os"].startswith("ubuntu"):
            assert disk >= 15, f"{node['id']}: below the 15 GB ubuntu-lts golden image"
