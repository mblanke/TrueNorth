"""Every shipped range template routes between its networks the way the worker builds it.

PR #230 (range diagrams) found the Medium Enterprise firewall declared no ``interfaces``:
the vSphere build gave it one NIC on ``management`` and nothing routed between the range's
four networks, while every VM's default gateway (worker/render.py: the subnet's ``.1``)
pointed at an address nobody held. Red vs Blue had the same fault across six zones. This
renders each template with the worker's own code and checks, per template:

* no two networks overlap; every declared address (node ``ip``, interface ``ip``) is
  inside its network; no address is used twice; interfaces name defined networks;
* every VM's gateway is an address a router/firewall NIC holds on that network, so its
  default route goes somewhere;
* the routers join every network into one routed whole, unless a network says
  ``isolated: true`` (an intentional enclave, which then needs no gateway);
* the edge (the appliance the worker gives the WAN uplink, vsphere_infra.pick_edge) is a
  router on every routed network, and each appliance's pfSense/VyOS config builds with
  the template's rules, none skipped;
* ``firewall_rules`` name defined zones; a network sensor (Security Onion, Zeek,
  Suricata, packet capture) has a monitor NIC or sits on a mirror destination.

``KNOWN`` lists findings that are open design questions, not settled bugs. The test
fails on any finding not listed and on any listed finding that has gone away, so the
list cannot rot.
"""

from __future__ import annotations

import copy
import ipaddress
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "control-plane/worker"))

from worker import pfsense_config, vyos_config  # noqa: E402
from worker.provisioners import vsphere_infra as infra  # noqa: E402
from worker.render import _is_router, render_topology  # noqa: E402

TEMPLATES = sorted((ROOT / "content/ranges").glob("*/template.yaml"))
RANGE_ID = "abcdef12-3456-7890-abcd-ef1234567890"
SENSOR_ROLES = frozenset({"network_monitor", "network_sensor", "network_ids", "ids_sensor", "ids_ips",
                          "packet_capture"})

# Open design questions, each an exact finding (none at present).
#
# Not findings, by design until issue #235 (chained firewalls / transit networks) lands:
# large-enterprise's ot_scada is `isolated: true` (ot-fw01 has no leg toward fw01), and
# red-vs-blue's perimeter firewalls fw01/fw02 sit on management only, off the traffic
# path, while rtr01 routes every zone. A single-NIC appliance raises no finding here;
# test_issue_235_stopgaps_are_still_in_place below fails if either changes unannounced.
KNOWN: dict[str, set[str]] = {}


def _host(vm: dict) -> str:
    return vm["name"][len(RANGE_ID[:8]) + 1:]


def _declared_addresses(template: dict, cidrs: dict[str, ipaddress.IPv4Network]) -> list[str]:
    """The template's own addresses that fall outside their network, or name none."""
    out: list[str] = []
    for node in template.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nid, own = node.get("id"), str(node.get("vlan", ""))
        if node.get("ip") and own in cidrs and ipaddress.ip_address(str(node["ip"])) not in cidrs[own]:
            out.append(f"{nid}: ip {node['ip']} is outside {own} {cidrs[own]}")
        for iface in node.get("interfaces") or []:
            name = str(iface.get("vlan")) if isinstance(iface, dict) else str(iface)
            if name not in cidrs:
                out.append(f"{nid}: interface on undefined network {name!r} (the worker drops it)")
            elif isinstance(iface, dict) and iface.get("ip") and \
                    ipaddress.ip_address(str(iface["ip"])) not in cidrs[name]:
                out.append(f"{nid}: interface ip {iface['ip']} is outside {name} {cidrs[name]}")
    return out


def check(template: dict) -> list[str]:
    """Every routing finding for one template (an empty list: it builds a routed range)."""
    built = render_topology(copy.deepcopy(template), RANGE_ID, lambda alias: alias)
    vms, nets = built["vm_definitions"], built["network_definitions"]
    net = template.get("network") if isinstance(template.get("network"), dict) else {}
    declared = {str(v.get("name")): v for v in net.get("vlans") or [] if isinstance(v, dict)}
    isolated = {name for name, v in declared.items() if v.get("isolated") is True}
    cidrs = {n["name"]: ipaddress.ip_network(n["cidr"]) for n in nets}
    findings: list[str] = []

    names = list(cidrs)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if cidrs[a].overlaps(cidrs[b]):
                findings.append(f"networks {a} {cidrs[a]} and {b} {cidrs[b]} overlap")
    findings += _declared_addresses(template, cidrs)

    seen: dict[str, str] = {}
    for vm in vms:
        for nic in vm["nics"]:
            if nic.get("ip"):
                if nic["ip"] in seen and seen[nic["ip"]] != _host(vm):
                    findings.append(f"address {nic['ip']} is used by {seen[nic['ip']]} and {_host(vm)}")
                seen.setdefault(nic["ip"], _host(vm))

    routers = [vm for vm in vms if _is_router({"role": vm["role"]}, vm["os"])]
    held: dict[str, set[str]] = {}  # network -> addresses a router NIC holds there
    for vm in routers:
        for nic in vm["nics"]:
            if nic.get("ip") and not nic.get("monitor"):
                held.setdefault(nic["network"], set()).add(nic["ip"])

    for vm in vms:
        if vm in routers:
            continue
        name = vm["nics"][0]["network"] if vm["nics"][0]["network"] in cidrs else None  # its own VLAN
        if name is None:
            findings.append(f"VM {_host(vm)} is on no network the template defines")
        elif name not in isolated and vm["gateway"] not in held.get(name, set()):
            findings.append(f"VM {_host(vm)} on {name}: gateway {vm['gateway']} is no router/firewall address")
    for n in nets:
        if n["name"] not in isolated and n["gateway"] not in held.get(n["name"], set()):
            findings.append(f"network {n['name']} has no router/firewall holding its gateway {n['gateway']}")

    # Networks joined by a router with addressed NICs on both (union-find).
    parent = {name: name for name in cidrs}

    def root(x: str) -> str:
        while parent[x] != x:
            x = parent[x]
        return x

    for vm in routers:
        linked = [nic["network"] for nic in vm["nics"] if nic.get("ip") and not nic.get("monitor")
                  and nic["network"] in parent]
        for other in linked[1:]:
            parent[root(other)] = root(linked[0])
    routed = [name for name in cidrs if name not in isolated]
    if len(routed) > 1:
        sizes: dict[str, int] = {}
        for name in routed:  # dicts keep order: ties go to the component of the earlier network
            sizes[root(name)] = sizes.get(root(name), 0) + 1
        main = max(sizes, key=lambda r: sizes[r])
        if sizes[main] == 1:
            main = ""  # nothing joins any two networks: every one of them is cut off
        findings += [f"network {name} is not routed to the rest of the range (mark it isolated: true if intended)"
                     for name in routed if root(name) != main]

    edge = infra.pick_edge(copy.deepcopy(vms))
    if edge is None:
        if len(routed) > 1:
            findings.append(f"the range has {len(routed)} routed networks and no edge appliance")
    else:
        on = {nic["network"] for nic in edge["nics"] if nic.get("ip")}
        missing = [name for name in routed if name not in on]
        if missing:
            findings.append(f"edge {edge['node_id']} has no NIC on {', '.join(missing)}")

    rules = [r for r in net.get("firewall_rules") or [] if isinstance(r, dict)]
    for r in rules:
        for side in ("src", "dst"):
            zone = str(r.get(side) or "*")
            if zone not in ("*", "any") and zone not in cidrs:
                findings.append(f"firewall rule {r.get('name')!r}: {side} {zone!r} is not a network")
    for vm in vms:
        if infra.os_family(vm) != "appliance":
            continue
        kw = {"networks": nets, "rules": rules, "depot_host": "192.0.2.10", "hostname": _host(vm)}
        if edge is not None and vm["node_id"] == edge["node_id"]:  # as vsphere_api._plan_uplink
            vm = copy.deepcopy(vm)
            vm["nics"].insert(0, {"uplink": True, "vlan": None, "network": "uplink", "ip": "192.0.2.50",
                                  "prefix": 24, "netmask": "255.255.255.0", "gateway": "192.0.2.1"})
        try:
            if product := pfsense_config.product_of(vm):
                cfg = pfsense_config.build_config(vm, product=product, **kw)
            elif vyos_config.is_vyos(vm):
                cfg = vyos_config.build_config(vm, **kw)
            else:
                continue
        except (pfsense_config.PfsenseConfigError, vyos_config.VyosConfigError) as exc:
            findings.append(f"appliance {vm['node_id']}: config does not build ({exc})")
            continue
        if edge is not None and vm["node_id"] == edge["node_id"]:
            findings += [f"edge {vm['node_id']}: {note}" for note in cfg.notes]

    mirror_dsts = {str(r.get("dst")) for r in infra.mirror_rules(template)}
    for vm in vms:
        if "securityonion" not in vm["os"].lower() and vm["role"] not in SENSOR_ROLES:
            continue
        monitor = any(nic.get("monitor") and nic["network"] in mirror_dsts for nic in vm["nics"])
        if not monitor and vm["nics"][0]["network"] not in mirror_dsts:
            findings.append(f"sensor {vm['node_id']} has no monitor NIC and is not on a mirror destination")
    return findings


def test_there_are_templates_to_check():
    assert len(TEMPLATES) >= 8


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.parent.name)
def test_template_routes_between_its_networks(path):
    findings = check(yaml.safe_load(path.read_text()))
    known = KNOWN.get(path.parent.name, set())
    assert [f for f in findings if f not in known] == [], f"{path.parent.name}: new routing findings"
    assert sorted(known - set(findings)) == [], f"{path.parent.name}: fixed; remove these from KNOWN"


def test_known_entries_name_shipped_templates():
    assert set(KNOWN) <= {p.parent.name for p in TEMPLATES}


def _load(name: str) -> dict:
    return yaml.safe_load((ROOT / "content/ranges" / name / "template.yaml").read_text())


def _forward_verdict(commands: list[str], src: str, dst: str, port: int) -> tuple[str, str]:
    """(action, rule description) VyOS's forward filter gives a new TCP/UDP flow: rules in
    number order, first match wins, default drop (worker/vyos_config.py)."""
    zones = [ipaddress.ip_network(c.split("'")[1]) for c in commands
             if c.startswith("set firewall group network-group TN_ZONES network ")]
    rules: dict[int, dict] = {}
    for c in commands:
        m = re.match(r"set firewall ipv4 forward filter rule (\d+) (.+)", c)
        if m:
            rules.setdefault(int(m.group(1)), {})[m.group(2).rsplit(" ", 1)[0]] = m.group(2).rsplit(" ", 1)[1]
    s, d = ipaddress.ip_address(src), ipaddress.ip_address(dst)

    def side(rule: dict, which: str, addr) -> bool:
        if f"{which} address" in rule:
            return addr in ipaddress.ip_network(rule[f"{which} address"].strip("'"))
        if rule.get(f"{which} group network-group") == "'TN_ZONES'":
            return any(addr in z for z in zones)
        return True

    def port_ok(rule: dict) -> bool:
        if "destination port" not in rule:
            return True
        for p in rule["destination port"].strip("'").split(","):
            lo, _, hi = p.partition("-")
            if int(lo) <= port <= int(hi or lo):
                return True
        return False

    for n in sorted(rules):
        r = rules[n]
        if "state" in r or "destination group address-group" in r:
            continue  # established/related, and the depot rule
        if side(r, "source", s) and side(r, "destination", d) and port_ok(r):
            return r["action"].strip("'"), r.get("description", "").strip("'")
    return "drop", "default"


def test_red_vs_blue_rules_allow_domain_join_and_logging_and_keep_red_out():
    t = _load("red-vs-blue")
    built = render_topology(copy.deepcopy(t), RANGE_ID, lambda alias: alias)
    rtr = next(vm for vm in built["vm_definitions"] if vm["node_id"] == "rtr01")
    cfg = vyos_config.build_config(rtr, networks=built["network_definitions"], rules=t["network"]["firewall_rules"])
    assert cfg.notes == []
    ip = {n["id"]: n["ip"] for n in t["nodes"]}

    def verdict(src, dst, port):
        return _forward_verdict(cfg.commands, ip[src], ip[dst], port)

    for port in (53, 88, 123, 135, 389, 445, 464, 636, 49152, 50000, 65535):  # domain join and logon
        assert verdict("ws01", "dc01", port) == ("accept", "corp-to-server-ad"), port
    assert verdict("lnx01", "db01", 3306) == ("accept", "corp-to-server")
    assert verdict("ws01", "dc01", 3389)[0] == "drop"  # nothing else from corp to the servers
    for src in ("ws01", "web01", "dc01", "blue01", "honey01"):  # every zone ships logs
        for port in (514, 5044, 9200):
            assert verdict(src, "siem01", port)[0] == "accept", (src, port)
    assert verdict("web01", "siem01", 22)[0] == "drop"  # logs only
    # Red stays in its enclave: the deny rules come before the log rule.
    for port in (514, 5044, 9200, 443):
        assert verdict("red-op01", "siem01", port) == ("drop", "red-to-mgmt-deny"), port
    assert verdict("red-op01", "dc01", 88) == ("drop", "red-to-server-deny")
    assert verdict("red-op01", "ws01", 445) == ("drop", "red-to-corp-deny")
    assert verdict("red-op01", "web01", 443) == ("accept", "red-to-dmz")


def test_issue_235_stopgaps_are_still_in_place():
    """The worker cannot chain appliances yet (issue #235). Until it can, OT is an enclave
    and Red vs Blue's perimeter firewalls are off the path; whoever fixes #235 updates both
    templates and this test together."""
    le = _load("large-enterprise")
    ot = next(v for v in le["network"]["vlans"] if v["name"] == "ot_scada")
    assert ot.get("isolated") is True
    otfw = next(n for n in le["nodes"] if n["id"] == "ot-fw01")
    assert not otfw.get("interfaces")
    findings = check(le)
    assert not [f for f in findings if "ot_scada" in f]
    # Without the flag the gap is a finding again: the enclave is a declared choice.
    del ot["isolated"]
    assert "network ot_scada is not routed to the rest of the range (mark it isolated: true if intended)" \
        in check(le)

    rvb = _load("red-vs-blue")
    for fw in ("fw01", "fw02"):
        node = next(n for n in rvb["nodes"] if n["id"] == fw)
        assert node["vlan"] == "management" and not node.get("interfaces")
    assert next(n for n in rvb["nodes"] if n["id"] == "rtr01").get("edge") is True


# ── the checks catch the faults they are for ───────────────────────────


def _two_zone(**fw) -> dict:
    return {
        "network": {"vlans": [{"id": 10, "name": "lan", "cidr": "10.1.10.0/24"},
                              {"id": 20, "name": "srv", "cidr": "10.1.20.0/24"}]},
        "nodes": [
            {"id": "ws01", "role": "workstation", "os": "windows-11", "vlan": "lan", "ip": "10.1.10.10"},
            {"id": "dc01", "role": "domain_controller", "os": "windows-server-2022", "vlan": "srv",
             "ip": "10.1.20.10"},
            {"id": "fw01", "role": "firewall", "os": "pfsense", "vlan": "srv", "ip": "10.1.20.1", **fw},
        ],
    }


def test_a_firewall_on_every_zone_passes():
    assert check(_two_zone(interfaces=[{"vlan": "lan", "ip": "10.1.10.1"}, {"vlan": "srv", "ip": "10.1.20.1"}])) == []


def test_a_firewall_without_interfaces_fails():
    findings = check(_two_zone())
    assert "VM ws01 on lan: gateway 10.1.10.1 is no router/firewall address" in findings
    assert "network lan has no router/firewall holding its gateway 10.1.10.1" in findings
    assert "network lan is not routed to the rest of the range (mark it isolated: true if intended)" in findings
    assert "edge fw01 has no NIC on lan" in findings


def test_isolated_intent_is_accepted():
    t = _two_zone()
    t["network"]["vlans"][0]["isolated"] = True
    assert check(t) == []


def test_a_gateway_off_the_zone_gateway_fails():
    # The firewall's lan NIC at .254: VMs still route to .1 (render.py), which nobody holds.
    findings = check(_two_zone(interfaces=[{"vlan": "lan", "ip": "10.1.10.254"}, {"vlan": "srv"}]))
    assert "VM ws01 on lan: gateway 10.1.10.1 is no router/firewall address" in findings


def test_an_address_outside_its_subnet_fails():
    findings = check(_two_zone(interfaces=[{"vlan": "lan", "ip": "10.1.99.1"}, {"vlan": "srv"}]))
    assert "fw01: interface ip 10.1.99.1 is outside lan 10.1.10.0/24" in findings


def test_overlap_undefined_interface_and_bad_rule_fail():
    t = _two_zone(interfaces=[{"vlan": "lan"}, {"vlan": "srv"}, {"vlan": "nope"}])
    t["network"]["vlans"][1]["cidr"] = "10.1.0.0/16"
    t["nodes"][1]["ip"] = "10.1.20.10"
    t["network"]["firewall_rules"] = [{"name": "r1", "src": "lan", "dst": "dmz", "action": "allow"}]
    findings = check(t)
    assert "networks lan 10.1.10.0/24 and srv 10.1.0.0/16 overlap" in findings
    assert "fw01: interface on undefined network 'nope' (the worker drops it)" in findings
    assert "firewall rule 'r1': dst 'dmz' is not a network" in findings


def test_a_vpn_box_picked_as_edge_fails():
    t = _two_zone(interfaces=[{"vlan": "lan", "ip": "10.1.10.1"}, {"vlan": "srv", "ip": "10.1.20.1"}])
    t["nodes"].insert(0, {"id": "vpn01", "role": "vpn_gateway", "os": "pfsense", "vlan": "lan", "ip": "10.1.10.40"})
    assert check(t) == ["edge vpn01 has no NIC on srv"]
    t["nodes"][-1]["edge"] = True
    assert check(t) == []


def test_a_sensor_without_a_monitor_nic_fails():
    t = _two_zone(interfaces=[{"vlan": "lan", "ip": "10.1.10.1"}, {"vlan": "srv", "ip": "10.1.20.1"}])
    t["nodes"].append({"id": "so01", "role": "network_monitor", "os": "securityonion", "vlan": "srv",
                       "ip": "10.1.20.60"})
    assert check(t) == ["sensor so01 has no monitor NIC and is not on a mirror destination"]
    t["network"]["vlans"].append({"id": 30, "name": "mon", "cidr": "10.1.30.0/24"})
    t["network"]["firewall_rules"] = [{"name": "span", "src": "*", "dst": "mon", "action": "mirror"}]
    t["nodes"][-1]["interfaces"] = [{"vlan": "srv"}, {"vlan": "mon", "monitor": True}]
    t["nodes"][2]["interfaces"].append({"vlan": "mon", "ip": "10.1.30.1"})
    assert check(t) == []


def test_the_shipped_medium_enterprise_before_the_fix_fails():
    t = yaml.safe_load((ROOT / "content/ranges/medium-enterprise/template.yaml").read_text())
    fw = next(n for n in t["nodes"] if n["id"] == "fw01")
    del fw["interfaces"]
    findings = check(t)
    for zone in ("corporate", "servers", "dmz"):
        assert f"network {zone} is not routed to the rest of the range (mark it isolated: true if intended)" in findings
