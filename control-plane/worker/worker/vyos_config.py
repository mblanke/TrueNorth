"""TrueNorth Range - a per-range VyOS configuration, rendered by the worker.

The VyOS counterpart of pfsense_config.py. Pure functions: the vSphere provisioner calls
:func:`build_config` with one rendered VyOS VM (its ``nics``, WAN first when the range has
an uplink), the range's networks and the template's ``network.firewall_rules``, then
:func:`guestinfo` to turn the result into cloud-init's VMware datasource keys
(``guestinfo.metadata`` / ``guestinfo.userdata``) before the VM's first power-on. VyOS
images carry cloud-init; its ``cc_vyos`` module runs the ``vyos_config_commands`` of the
user data once per instance id, commits and saves them (build guide, VyOS).

The policy is the pfSense one, in VyOS 1.4+ syntax:

* interfaces in NIC order, ``eth0`` = the uplink (static, the reserved address, default
  route to the uplink gateway) when there is one, then one per zone at the zone's gateway
  address. Each is pinned to its NIC by MAC (``hw-id``), so the guest's numbering cannot
  swap zones.
* source NAT (masquerade) of every range subnet out of ``eth0`` when there is an uplink.
* forward filter, default drop: replies (established/related); every zone to the depot
  (``TN_DEPOT`` on ``TN_DEPOT_PORTS``) when there is an uplink and a depot; then the
  template's ``firewall_rules`` in template order, or (none in the template) every zone to
  every zone. ``dst: "*"`` means every range zone, never the internet. So nothing leaves
  the range but depot traffic.
* input filter on the uplink: replies only (nothing reaches the router from the WAN).

No secrets: the template's users and SSH settings stay as they are.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
from dataclasses import dataclass, field

import yaml

DEPOT_PORTS = (8081, 3142)
# The VMX limit for one guestinfo value is far above a range config (a few KiB).
MAX_GUESTINFO = 60_000
_SAFE = re.compile(r"[^A-Za-z0-9 _.:/,-]")


class VyosConfigError(ValueError):
    """The VM cannot be given a VyOS config (no zones, or it would not fit guestinfo)."""


def is_vyos(vm_def: dict) -> bool:
    name = str(vm_def.get("os") or "").lower() + " " + str(vm_def.get("template_name") or "").lower()
    return "vyos" in name


@dataclass(frozen=True)
class Interface:
    ifname: str  # ethN, in vSphere NIC order (pinned by hw-id)
    network: str
    ip: str
    prefix: int
    cidr: str
    uplink: bool = False


@dataclass(frozen=True)
class VyosConfig:
    commands: list[str]  # configuration-mode "set ..." lines, without the hw-id pins
    interfaces: list[Interface]
    hostname: str
    notes: list[str] = field(default_factory=list)

    @property
    def sha256(self) -> str:
        return hashlib.sha256("\n".join(self.commands).encode()).hexdigest()

    def summary(self) -> dict:
        """What the provision result records about this router's config."""
        return {
            "delivery": "cloud-init guestinfo",
            "config_sha256": self.sha256,
            "interfaces": [{"if": i.ifname, "network": i.network, "ip": i.ip, "prefix": i.prefix,
                            **({"uplink": True} if i.uplink else {})} for i in self.interfaces],
        }


def _q(text: object) -> str:
    """A single-quoted VyOS value; quotes and shell metacharacters are dropped."""
    return "'" + _SAFE.sub("", str(text))[:200] + "'"


def _interfaces(vm_def: dict) -> list[Interface]:
    out: list[Interface] = []
    for i, nic in enumerate(vm_def.get("nics") or []):
        if nic.get("uplink") and i != 0:
            raise VyosConfigError("the uplink NIC must be NIC 0 (eth0)")
        ip = str(nic.get("ip") or "")
        prefix = int(nic.get("prefix") or 24)
        cidr = str(ipaddress.ip_interface(f"{ip}/{prefix}").network) if ip else ""
        out.append(Interface(f"eth{i}", str(nic.get("network") or ""), ip, prefix, cidr, bool(nic.get("uplink"))))
    return out


def _ports(rule: dict) -> list[str]:
    ports = rule.get("ports") or []
    if isinstance(ports, (int, str)):
        ports = [ports]
    return [str(p).strip() for p in ports if str(p).strip()]


def build_config(
    vm_def: dict,
    *,
    networks: list[dict],
    rules: list[dict] | None = None,
    depot_host: str = "",
    depot_ports: tuple[int, ...] | list[int] = DEPOT_PORTS,
    domain: str = "range.local",
    hostname: str = "",
) -> VyosConfig:
    """The VyOS configuration for one rendered router VM (see the module docstring)."""
    ifaces = _interfaces(vm_def)
    uplink = next((i for i in ifaces if i.uplink), None)
    zones = [i for i in ifaces if not i.uplink]
    if not zones or not all(z.ip for z in zones):
        raise VyosConfigError(f"router {vm_def.get('name')!r} has a zone NIC without an address")
    name = (hostname or str(vm_def.get("node_id") or vm_def.get("name") or "vyos"))[:63]
    notes: list[str] = []
    cmds = [f"set system host-name {_q(name)}", f"set system domain-name {_q(domain)}"]
    for i in ifaces:
        cmds.append(f"set interfaces ethernet {i.ifname} address {_q(f'{i.ip}/{i.prefix}')}")
        cmds.append(f"set interfaces ethernet {i.ifname} description {_q('WAN' if i.uplink else i.network)}")
    gateway = str((vm_def.get("nics") or [{}])[0].get("gateway") or "") if uplink else ""
    if gateway:
        cmds.append(f"set protocols static route 0.0.0.0/0 next-hop {_q(gateway)}")

    range_cidrs: list[str] = []
    for c in [str(n.get("cidr") or "") for n in networks] + [z.cidr for z in zones]:
        if c and c not in range_cidrs:
            range_cidrs.append(c)
    cmds += [f"set firewall group network-group TN_ZONES network {_q(c)}" for c in range_cidrs]
    by_zone = {z.network: z for z in zones}
    cidr_by_name = {str(n.get("name")): str(n.get("cidr") or "") for n in networks if n.get("name")}

    if uplink:
        cmds += [
            f"set nat source rule 100 outbound-interface name {_q(uplink.ifname)}",
            "set nat source rule 100 source group network-group 'TN_ZONES'",
            "set nat source rule 100 translation address 'masquerade'",
            "set firewall ipv4 input filter rule 1 action 'accept'",
            "set firewall ipv4 input filter rule 1 state 'established'",
            "set firewall ipv4 input filter rule 1 state 'related'",
            "set firewall ipv4 input filter rule 2 action 'drop'",
            f"set firewall ipv4 input filter rule 2 inbound-interface name {_q(uplink.ifname)}",
        ]

    fwd = "set firewall ipv4 forward filter"
    cmds += [f"{fwd} default-action 'drop'", f"{fwd} rule 1 action 'accept'",
             f"{fwd} rule 1 state 'established'", f"{fwd} rule 1 state 'related'"]
    if uplink and depot_host:
        try:
            ipaddress.ip_address(depot_host)
        except ValueError:
            notes.append(f"depot {depot_host!r} is not an IP address; VyOS needs one (no depot rule)")
        else:
            cmds += [f"set firewall group address-group TN_DEPOT address {_q(depot_host)}"]
            cmds += [f"set firewall group port-group TN_DEPOT_PORTS port {_q(p)}" for p in depot_ports]
            cmds += [f"{fwd} rule 10 action 'accept'", f"{fwd} rule 10 description 'TrueNorth: range to depot only'",
                     f"{fwd} rule 10 protocol 'tcp'", f"{fwd} rule 10 source group network-group 'TN_ZONES'",
                     f"{fwd} rule 10 destination group address-group 'TN_DEPOT'",
                     f"{fwd} rule 10 destination group port-group 'TN_DEPOT_PORTS'"]

    # Greyspace (worker/greyspace_host.py route_router): the simulated internet's public
    # space is routed to gs-core, and every zone may reach it (it is the range's internet).
    gs = vm_def.get("greyspace_route") if isinstance(vm_def.get("greyspace_route"), dict) else None
    if gs:
        try:
            prefix, via = ipaddress.ip_network(str(gs["prefix"])), ipaddress.ip_address(str(gs["via"]))
        except (KeyError, ValueError):
            notes.append(f"greyspace route {gs!r} is not a prefix and an address (skipped)")
        else:
            cmds += [f"set protocols static route {_q(str(prefix))} next-hop {_q(str(via))}",
                     f"{fwd} rule 20 action 'accept'", f"{fwd} rule 20 description 'TrueNorth: zones to Greyspace'",
                     f"{fwd} rule 20 source group network-group 'TN_ZONES'",
                     f"{fwd} rule 20 destination address {_q(str(prefix))}"]
            if gs.get("resolver"):
                cmds += [f"set system name-server {_q(str(gs['resolver']))}"]

    number = 100
    if rules:
        for n, rule in enumerate(rules):
            rname = str(rule.get("name") or f"rule{n + 1}")
            action = str(rule.get("action") or "allow").lower()
            if action == "mirror":
                continue  # SPAN: a vDS port-mirroring session, not a filter rule
            verdict = {"allow": "accept", "pass": "accept", "deny": "drop", "block": "drop",
                       "reject": "reject"}.get(action)
            if verdict is None:
                notes.append(f"firewall rule {rname!r}: action {action!r} is not a VyOS filter rule (skipped)")
                continue
            src, dst = str(rule.get("src") or "*"), str(rule.get("dst") or "*")
            if src in ("*", "any"):
                src_kw = "source group network-group 'TN_ZONES'"
            elif src in by_zone or cidr_by_name.get(src):
                src_kw = f"source address {_q(by_zone[src].cidr if src in by_zone else cidr_by_name[src])}"
            else:
                notes.append(f"firewall rule {rname!r}: unknown source zone {src!r} (skipped)")
                continue
            if dst in ("*", "any"):
                dst_kw = "destination group network-group 'TN_ZONES'"
            elif dst in by_zone or cidr_by_name.get(dst):
                dst_kw = f"destination address {_q(by_zone[dst].cidr if dst in by_zone else cidr_by_name[dst])}"
            else:
                notes.append(f"firewall rule {rname!r}: unknown destination zone {dst!r} (skipped)")
                continue
            r = f"{fwd} rule {number}"
            cmds += [f"{r} action '{verdict}'", f"{r} description {_q(rname)}", f"{r} {src_kw}", f"{r} {dst_kw}"]
            if ports := _ports(rule):
                cmds += [f"{r} protocol 'tcp_udp'", f"{r} destination port {_q(','.join(ports))}"]
            number += 10
    else:
        r = f"{fwd} rule {number}"
        cmds += [f"{r} action 'accept'", f"{r} description 'every zone to every zone'",
                 f"{r} source group network-group 'TN_ZONES'", f"{r} destination group network-group 'TN_ZONES'"]
    return VyosConfig(commands=cmds, interfaces=ifaces, hostname=name, notes=notes)


def guestinfo(cfg: VyosConfig, macs: list[str], instance: str) -> dict[str, str]:
    """cloud-init's VMware datasource keys for a VM whose NICs (device-key order) have
    ``macs``: each ethN is pinned to its MAC first, then the configuration. The instance
    id carries the config's hash, so a changed config is applied again."""
    if len(macs) != len(cfg.interfaces):
        raise VyosConfigError(f"the VM has {len(macs)} NICs; the VyOS config expects {len(cfg.interfaces)}")
    pins = [f"set interfaces ethernet {i.ifname} hw-id {_q(mac.lower())}"
            for i, mac in zip(cfg.interfaces, macs, strict=True)]
    userdata = "#cloud-config\n" + yaml.safe_dump({"vyos_config_commands": pins + cfg.commands}, sort_keys=False,
                                                  width=1000)
    metadata = yaml.safe_dump({"instance-id": f"{instance}-{cfg.sha256[:12]}", "local-hostname": cfg.hostname},
                              sort_keys=False)

    def b64(text: str) -> str:
        return base64.b64encode(text.encode()).decode()

    values = {"guestinfo.metadata": b64(metadata), "guestinfo.metadata.encoding": "base64",
              "guestinfo.userdata": b64(userdata), "guestinfo.userdata.encoding": "base64"}
    if len(values["guestinfo.userdata"]) > MAX_GUESTINFO:
        raise VyosConfigError(f"the VyOS config is {len(values['guestinfo.userdata'])} bytes encoded; "
                              f"guestinfo takes {MAX_GUESTINFO}")
    return values


def commands_of(values: dict[str, str]) -> list[str]:
    """The configuration commands in guestinfo values (tests, troubleshooting)."""
    data = yaml.safe_load(base64.b64decode(values["guestinfo.userdata"]).decode().split("\n", 1)[1])
    return list(data["vyos_config_commands"])
