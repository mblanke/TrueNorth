"""TrueNorth Range - a per-range pfSense ``config.xml``, rendered by the worker.

Pure functions: the vSphere provisioner calls :func:`build_config` with one rendered
pfSense VM (its ``nics``, WAN first when the range has an uplink), the range's networks
and the template's ``network.firewall_rules``, then :func:`guestinfo` to turn the result
into the VM's ``guestinfo.tn.pfsense.*`` keys before its first power-on. Inside the
guest, ``tn-pfsense-config`` (infra/vsphere/packer/files/pfsense/, baked into the
template and run as an ``earlyshellcmd``) reads those keys, writes ``/conf/config.xml``
and reboots once into it.

What the config holds:

* interfaces in NIC order: ``wan`` = the uplink NIC (static, the address the worker
  reserved, gateway ``WANGW``) when there is one, then ``lan``, ``opt1``… one per zone,
  each at the zone's gateway address. Without an uplink the first zone takes the ``wan``
  slot (pfSense always has one) with no gateway and no NAT.
* outbound NAT on WAN (automatic), the DNS resolver on the zone interfaces.
* rules, per zone interface: DNS to the firewall; the depot (``TN_DEPOT`` on
  ``TN_DEPOT_PORTS``) when there is an uplink and a depot; then the template's
  ``firewall_rules`` whose ``src`` is that zone, in template order, or (no rules in the
  template) every zone to every zone. ``dst: "*"`` means every range zone, never the
  internet. Anything else is pfSense's default deny.
* two floating rules on WAN, evaluated before everything else: pass out to the depot
  ports, block out everything else. Whatever the zone rules say, nothing leaves the range
  but depot traffic.

No secrets: the guest keeps the template's users, groups, web GUI certificate and SSH
settings (the boot script copies them over from the config it replaces), so the admin
password stays the template's and nothing a vCenter reader can see in guestinfo is a
credential.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import ipaddress
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# The template's boot hook (infra/vsphere/packer/files/pfsense/tn-pfsense-config).
# Every generated config carries it too, so later boots (and a changed guestinfo) still apply.
BOOT_COMMAND = "/usr/local/bin/php -q /usr/local/sbin/tn-pfsense-config boot"
GUESTINFO_CONFIG = "guestinfo.tn.pfsense.config"
GUESTINFO_IFMAP = "guestinfo.tn.pfsense.ifmap"
# pfSense CE 2.7.2's config version. The boot script replaces it with the version of the
# config it finds in the guest, so a newer or older pfSense template still upgrades cleanly.
CONFIG_VERSION = "23.3"
DEPOT_PORTS = (8081, 3142)  # Nexus (Chocolatey feed), apt-cacher-ng
# A guestinfo value comfortably under the VMX limits; a range config is a few KiB.
MAX_GUESTINFO = 60_000
_ALIAS_RE = re.compile(r"[^A-Za-z0-9_]")


class PfsenseConfigError(ValueError):
    """The VM cannot be given a pfSense config (no zones, or it would not fit guestinfo)."""


@dataclass(frozen=True)
class Interface:
    key: str  # wan | lan | optN
    ifname: str  # vmxN, in NIC order; the boot script renames by MAC if the guest differs
    network: str  # the zone (or uplink) name
    ip: str
    prefix: int
    cidr: str  # the subnet, "" for none


@dataclass(frozen=True)
class PfsenseConfig:
    xml: str
    interfaces: list[Interface]
    notes: list[str] = field(default_factory=list)  # template rules that were skipped, and why

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.xml.encode()).hexdigest()

    def summary(self) -> dict:
        """What the provision result records about this firewall's config."""
        return {
            "delivery": "guestinfo",
            "config_sha256": self.sha256,
            "interfaces": [
                {"name": i.key, "if": i.ifname, "network": i.network, "ip": i.ip, "prefix": i.prefix}
                for i in self.interfaces
            ],
        }


def is_pfsense(vm_def: dict) -> bool:
    name = str(vm_def.get("os") or "").lower() + " " + str(vm_def.get("template_name") or "").lower()
    return "pfsense" in name


def _alias_name(text: str, limit: int = 31) -> str:
    clean = _ALIAS_RE.sub("_", text).strip("_") or "x"
    return clean[:limit]


def _sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def _interfaces(vm_def: dict) -> list[Interface]:
    nics = vm_def.get("nics") or []
    has_uplink = bool(nics) and bool(nics[0].get("uplink"))
    zone_keys = iter(["lan"] + [f"opt{i}" for i in range(1, 64)])
    out: list[Interface] = []
    for i, nic in enumerate(nics):
        if nic.get("uplink"):
            if i != 0:
                raise PfsenseConfigError("the uplink NIC must be NIC 0 (the WAN)")
            key = "wan"
        elif not has_uplink and i == 0:
            key = "wan"  # pfSense always has a WAN slot; an isolated range's first zone takes it
        else:
            key = next(zone_keys)
        ip = str(nic.get("ip") or "")
        prefix = int(nic.get("prefix") or 24)
        cidr = str(ipaddress.ip_interface(f"{ip}/{prefix}").network) if ip else ""
        out.append(Interface(key, f"vmx{i}", str(nic.get("network") or ""), ip, prefix, cidr))
    return out


def _endpoint(parent: ET.Element, tag: str, *, network: str = "", address: str = "", port: str = "") -> None:
    el = _sub(parent, tag)
    if network:
        _sub(el, "network", network)
    elif address:
        _sub(el, "address", address)
    else:
        _sub(el, "any")
    if port:
        _sub(el, "port", port)


class _Rules:
    def __init__(self, filter_el: ET.Element, aliases_el: ET.Element):
        self.filter = filter_el
        self.aliases = aliases_el
        self.tracker = 1_700_000_000
        self.alias_names: set[str] = set()

    def alias(self, name: str, kind: str, members: list[str], descr: str) -> str:
        name = _alias_name(name)
        if name not in self.alias_names:
            self.alias_names.add(name)
            a = _sub(self.aliases, "alias")
            _sub(a, "name", name)
            _sub(a, "type", kind)
            _sub(a, "address", " ".join(members))
            _sub(a, "descr", descr)
            _sub(a, "detail", "||".join([""] * len(members)))
        return name

    def rule(
        self,
        action: str,
        interface: str,
        descr: str,
        *,
        src_net: str = "",
        src_addr: str = "",
        dst_net: str = "",
        dst_addr: str = "",
        protocol: str = "",
        port: str = "",
        floating: bool = False,
        direction: str = "",
        log: bool = False,
    ) -> None:
        self.tracker += 1
        r = _sub(self.filter, "rule")
        _sub(r, "tracker", str(self.tracker))
        _sub(r, "type", action)
        _sub(r, "interface", interface)
        _sub(r, "ipprotocol", "inet")
        if floating:
            _sub(r, "floating", "yes")
            _sub(r, "quick", "yes")
            _sub(r, "direction", direction or "any")
        if protocol:
            _sub(r, "protocol", protocol)
        if action == "pass":
            _sub(r, "statetype", "keep state")
        _endpoint(r, "source", network=src_net, address=src_addr)
        _endpoint(r, "destination", network=dst_net, address=dst_addr, port=port)
        if log:
            _sub(r, "log")
        _sub(r, "descr", descr[:200])


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
) -> PfsenseConfig:
    """The pfSense config.xml for one rendered firewall VM (see the module docstring)."""
    ifaces = _interfaces(vm_def)
    has_uplink = bool(ifaces) and bool((vm_def.get("nics") or [{}])[0].get("uplink"))
    zones = [i for i in ifaces if not (has_uplink and i.key == "wan")]
    if not zones or not all(z.ip for z in zones):
        raise PfsenseConfigError(f"firewall {vm_def.get('name')!r} has a zone NIC without an address")
    notes: list[str] = []

    root = ET.Element("pfsense")
    _sub(root, "version", CONFIG_VERSION)
    system = _sub(root, "system")
    _sub(system, "optimization", "normal")
    _sub(system, "hostname", hostname or str(vm_def.get("node_id") or vm_def.get("name") or "pfsense")[:63])
    _sub(system, "domain", domain)
    _sub(system, "timezone", "Etc/UTC")
    _sub(system, "language", "en_US")
    _sub(system, "already_run_config_wizard")
    _sub(system, "disablenatreflection", "yes")
    _sub(system, "earlyshellcmd", BOOT_COMMAND)

    interfaces = _sub(root, "interfaces")
    for i in ifaces:
        el = _sub(interfaces, i.key)
        _sub(el, "enable")
        _sub(el, "if", i.ifname)
        _sub(el, "descr", "WAN" if (has_uplink and i.key == "wan") else _alias_name(i.network or i.key, 22))
        _sub(el, "ipaddr", i.ip)
        _sub(el, "subnet", str(i.prefix))
        if has_uplink and i.key == "wan" and vm_def["nics"][0].get("gateway"):
            _sub(el, "gateway", "WANGW")

    if has_uplink and vm_def["nics"][0].get("gateway"):
        gws = _sub(root, "gateways")
        item = _sub(gws, "gateway_item")
        _sub(item, "interface", "wan")
        _sub(item, "gateway", vm_def["nics"][0]["gateway"])
        _sub(item, "name", "WANGW")
        _sub(item, "weight", "1")
        _sub(item, "ipprotocol", "inet")
        _sub(item, "descr", "Range uplink (depot network)")
        _sub(gws, "defaultgw4", "WANGW")

    _sub(root, "dhcpd")  # every range VM has a static address
    _sub(_sub(_sub(root, "nat"), "outbound"), "mode", "automatic" if has_uplink else "disabled")
    unbound = _sub(root, "unbound")
    _sub(unbound, "enable")
    _sub(unbound, "active_interface", ",".join(["lo0"] + [z.key for z in zones]))
    _sub(unbound, "outgoing_interface", ",".join(z.key for z in zones))
    _sub(unbound, "system_domain_local_zone_type", "transparent")
    _sub(_sub(root, "syslog"), "filterdescriptions", "1")

    aliases = _sub(root, "aliases")
    filt = _sub(root, "filter")
    fr = _Rules(filt, aliases)

    by_zone = {z.network: z for z in zones}
    range_cidrs: list[str] = []
    for n in networks:
        with_cidr = str(n.get("cidr") or "")
        if with_cidr and with_cidr not in range_cidrs:
            range_cidrs.append(with_cidr)
    for z in zones:
        if z.cidr not in range_cidrs:
            range_cidrs.append(z.cidr)
    all_zones = fr.alias("TN_ZONES", "network", range_cidrs, "Every subnet of this range")
    cidr_by_name = {str(n.get("name")): str(n.get("cidr") or "") for n in networks if n.get("name")}

    depot = ""
    depot_port_alias = ""
    if has_uplink:
        if depot_host:
            depot = fr.alias("TN_DEPOT", "host", [depot_host], "TN-DEPOT01, the software depot")
            depot_port_alias = fr.alias(
                "TN_DEPOT_PORTS", "port", [str(p) for p in depot_ports], "Depot services the range may use"
            )
            fr.rule(
                "pass",
                "wan",
                "TrueNorth: range -> depot only",
                dst_addr=depot,
                protocol="tcp",
                port=depot_port_alias,
                floating=True,
                direction="out",
            )
        fr.rule("block", "wan", "TrueNorth: nothing else leaves the range", floating=True, direction="out", log=True)

    for z in zones:
        fr.rule(
            "pass",
            z.key,
            f"{z.network}: DNS to the firewall",
            src_net=z.key,
            dst_net="(self)",
            protocol="tcp/udp",
            port="53",
        )
        if depot:
            fr.rule(
                "pass",
                z.key,
                f"{z.network}: software depot",
                src_net=z.key,
                dst_addr=depot,
                protocol="tcp",
                port=depot_port_alias,
            )

    if rules:
        for n, rule in enumerate(rules):
            name = str(rule.get("name") or f"rule{n + 1}")
            action = str(rule.get("action") or "allow").lower()
            if action == "mirror":
                continue  # SPAN: a vDS port-mirroring session (vsphere_infra), not a pf rule
            if action not in ("allow", "pass", "deny", "block", "reject"):
                notes.append(f"firewall rule {name!r}: action {action!r} is not a pfSense filter rule (skipped)")
                continue
            src = str(rule.get("src") or "*")
            dst = str(rule.get("dst") or "*")
            srcs = list(zones) if src in ("*", "any") else [by_zone[src]] if src in by_zone else []
            if not srcs:
                if src not in cidr_by_name:
                    notes.append(f"firewall rule {name!r}: unknown source zone {src!r} (skipped)")
                continue  # a zone behind another firewall in the range
            if dst in ("*", "any"):
                dst_kw = {"dst_addr": all_zones}
            elif dst in by_zone:
                dst_kw = {"dst_net": by_zone[dst].key}
            elif cidr_by_name.get(dst):
                dst_kw = {"dst_addr": cidr_by_name[dst]}
            else:
                notes.append(f"firewall rule {name!r}: unknown destination zone {dst!r} (skipped)")
                continue
            ports = _ports(rule)
            proto, port = "", ""
            if ports:
                proto = "tcp/udp"
                port = ports[0] if len(ports) == 1 else fr.alias(f"TN_P_{name}", "port", ports, name)
            kind = "pass" if action in ("allow", "pass") else "block" if action in ("deny", "block") else "reject"
            descr = f"{name}: {rule.get('note') or rule.get('description') or ''}".strip().rstrip(":")
            for z in srcs:
                fr.rule(kind, z.key, descr, src_net=z.key, protocol=proto, port=port, **dst_kw)
    else:
        for z in zones:
            fr.rule("pass", z.key, f"{z.network}: to every range zone", src_net=z.key, dst_addr=all_zones)

    ET.indent(root)
    xml = '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
    return PfsenseConfig(xml=xml, interfaces=ifaces, notes=notes)


def encode(xml: str) -> str:
    """gzip (no timestamp, so the same config encodes the same) then base64."""
    return base64.b64encode(gzip.compress(xml.encode(), mtime=0)).decode()


def guestinfo(cfg: PfsenseConfig, macs: list[str]) -> dict[str, str]:
    """``guestinfo.tn.pfsense.*`` for a VM whose NICs (device-key order) have ``macs``.

    ``ifmap`` lets the boot script rename ``vmxN`` to whatever the guest called the NIC
    with that MAC (FreeBSD numbers vmxnet3 cards by PCI slot, which can differ from the
    vSphere device order once a VM has more than a handful of NICs).
    """
    if len(macs) != len(cfg.interfaces):
        raise PfsenseConfigError(f"the VM has {len(macs)} NICs; the pfSense config expects {len(cfg.interfaces)}")
    blob = encode(cfg.xml)
    if len(blob) > MAX_GUESTINFO:
        raise PfsenseConfigError(f"the pfSense config is {len(blob)} bytes encoded; guestinfo takes {MAX_GUESTINFO}")
    ifmap = " ".join(f"{i.ifname}={mac.lower()}" for i, mac in zip(cfg.interfaces, macs, strict=True))
    return {GUESTINFO_CONFIG: blob, GUESTINFO_IFMAP: ifmap}


def decode(blob: str) -> str:
    return gzip.decompress(base64.b64decode(blob)).decode()
