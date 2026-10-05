"""Topology → VM-definitions renderer (worker-side, self-contained).

Converts a range template (`nodes` / `assets` + `network.vlans` / `inputs`) into
concrete `vm_definitions` the provisioners consume, resolving each node's OS alias
to a hypervisor template name via the golden-image registry and allocating static
IPs within each VLAN's CIDR. Vendored here (rather than importing the Redis-coupled
scenario-engine template_engine) so the worker stays self-contained.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import re
from collections.abc import Callable
from typing import Any

# Renamed OS identifiers and the spellings of one golden image. Vendored copy of the
# tables in control-plane/api/app/golden_images.py; tests/api/test_os_aliases.py
# fails if they drift apart.
DEPRECATED_OS_ALIASES: dict[str, str] = {
    "ubuntu-2204": "ubuntu-2404",
    "ubuntu2204": "ubuntu-2404",
    "ubuntu-22.04": "ubuntu-24.04",
}
EQUIVALENT_OS_ALIASES: tuple[tuple[str, ...], ...] = (
    ("ubuntu-2404", "ubuntu-24.04", "ubuntu-2204", "ubuntu-22.04", "ubuntu2204"),
)
DEFAULT_OS = "ubuntu-2404"


def canonical_os(os_alias: str) -> str:
    """Map a deprecated OS identifier to its current name; anything else is unchanged."""
    alias = (os_alias or "").strip()
    return DEPRECATED_OS_ALIASES.get(alias, alias)


def os_alias_candidates(os_alias: str) -> list[str]:
    """Identifiers to try, in order: the current name, as given, then its equivalents."""
    alias = (os_alias or "").strip()
    out = [canonical_os(alias), alias]
    for group in EQUIVALENT_OS_ALIASES:
        if alias in group:
            out.extend(group)
    return list(dict.fromkeys(a for a in out if a))


def _extract_vlans(t: dict) -> list[dict]:
    if isinstance(t.get("network"), dict) and t["network"].get("vlans"):
        return t["network"]["vlans"]
    segments: list[dict] = []
    for key, val in (t.get("inputs") or {}).items():
        if key.startswith("cidr_"):
            name = key[len("cidr_"):]
            segments.append({"name": name, "cidr": val, "description": name})
    return segments or [{"name": "default", "cidr": "10.0.0.0/24", "description": "Default"}]


# Range Designer exports written before the designer emitted `os`/`specs` carry the
# node's stencil type and `os_template`/`vcpu`/`ram_mb`/`disk_gb` instead. Zones,
# switches and clouds in those files are drawing, not VMs.
_DESIGNER_NON_VM_TYPES = frozenset({"switch", "cloud", "subnet", "dmz"})


def _node_os(node: dict) -> str:
    return str(node.get("os") or node.get("os_template") or node.get("type") or DEFAULT_OS)


def _node_specs(node: dict) -> dict:
    specs = dict(node.get("specs") or {}) if isinstance(node.get("specs"), dict) else {}
    for legacy, key in (("vcpu", "cores"), ("ram_mb", "memory_mb"), ("disk_gb", "disk_gb")):
        if key not in specs and node.get(legacy) not in (None, ""):
            with contextlib.suppress(TypeError, ValueError):
                specs[key] = int(node[legacy])
    return specs


def _extract_nodes(t: dict) -> list[dict]:
    if t.get("nodes"):
        return t["nodes"]
    if t.get("assets"):
        return [
            {"id": a.get("role", "vm"), "role": a.get("role", "generic"),
             "os": a.get("os", DEFAULT_OS), "type": a.get("type", "vm"),
             "count": a.get("count", 1), "tags": a.get("tags", []),
             "services": a.get("services", [])}
            for a in t["assets"]
        ]
    return []


# -- Background-noise management NIC -------------------------------------
# Which nodes get an agent mirrors control-plane/api/app/noise/topology.py (agent_nodes);
# tests/api/test_noise_topology.py fails if the two disagree. Their addresses are NOT
# derived here: the API reserves them when it accepts the provision (app/noise/mgmt.py)
# and passes them to provision_range. A node with no reserved address gets no NIC.
NOISE_AGENT_ROLES = frozenset({"workstation", "usersim", "traffic_generator"})
_NOISE_DEFAULT_EXCLUDE = re.compile(r"red|blue|soc|mgmt|management", re.I)
NOISE_DEFAULT_MGMT = {"vlan_id": 4001, "cidr": "10.255.0.0/24"}


def _noise_block(t: dict) -> dict:
    return t["noise"] if isinstance(t.get("noise"), dict) else {}


def _is_noise_agent(node: dict, block: dict) -> bool:
    opt = node.get("noise") if isinstance(node.get("noise"), dict) else {}
    if "agent" in opt:
        return bool(opt["agent"])
    vlan = str(node.get("vlan", ""))
    excluded = (
        (vlan in set(block.get("exclude_vlans") or []))
        if "exclude_vlans" in block
        else bool(_NOISE_DEFAULT_EXCLUDE.search(vlan))
    )
    return node.get("role") in NOISE_AGENT_ROLES and not excluded


def noise_mgmt_plan(template: dict, addresses: dict[str, str] | None = None) -> tuple[dict | None, dict[str, dict]]:
    """(management network definition, {vm hostname: mgmt nic}) for the agent nodes that
    hold a reserved address in ``addresses``. Empty unless noise is on and some do."""
    block = _noise_block(template)
    if not block.get("enabled") or not addresses:
        return None, {}
    mgmt = {**NOISE_DEFAULT_MGMT, **(block.get("mgmt") or {})}
    net = ipaddress.ip_network(mgmt["cidr"], strict=False)
    nics: dict[str, dict] = {}
    for node in template.get("nodes") or []:  # noise is a `nodes`-format feature only
        count = int(node.get("count", 1) or 1)
        for r in range(count):
            host = f"{node.get('id', 'vm')}-{r}" if count > 1 else str(node.get("id", "vm"))
            if _is_noise_agent(node, block) and addresses.get(host):
                nics[host] = {"vlan_id": int(mgmt["vlan_id"]), "ip": addresses[host], "prefix": net.prefixlen}
    if not nics:
        return None, {}
    network = {
        "name": "noise_mgmt",
        "vlan_id": int(mgmt["vlan_id"]),
        "cidr": str(net),
        "gateway": "",  # deliberately unrouted: it reaches the controller and nothing else
        "description": "Background-noise control channel (white cell, out of bounds)",
    }
    return network, nics


def render_topology(
    template: dict,
    range_id: str,
    resolve_template: Callable[[str], str | None],
    vlan_base: int = 100,
    noise_mgmt: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return {range_name, vm_definitions, network_definitions, vlan_map, unresolved}.

    Each vm_definition carries: name, node_id, role, os, template_name, vlan_id/vlan_tag,
    ip, gateway, netmask, prefix, cores, memory/memory_mb, disk_gb.
    """
    range_name = template.get("name") or template.get("id") or range_id

    vlans = _extract_vlans(template)
    vlan_map: dict[str, int] = {}
    networks: list[dict] = []
    net_by_name: dict[str, dict] = {}
    for offset, seg in enumerate(vlans):
        name = seg.get("name", f"seg{offset}")
        vid = int(seg["id"]) if str(seg.get("id", "")).strip().isdigit() else vlan_base + offset
        vlan_map[name] = vid
        cidr = seg.get("cidr") or f"10.{vlan_base}.{offset}.0/24"
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            net = ipaddress.ip_network("10.0.0.0/24")
        gw = str(net.network_address + 1)
        networks.append({"name": name, "vlan_id": vid, "cidr": str(net), "gateway": gw,
                         "description": seg.get("description", "")})
        net_by_name[name] = {"net": net, "gateway": gw, "next": int(net.network_address) + 10}

    nodes = _extract_nodes(template)
    vms: list[dict] = []
    unresolved: list[str] = []
    for node in nodes:
        if (not node.get("os") and not node.get("os_template")
                and str(node.get("type", "")) in _DESIGNER_NON_VM_TYPES):
            continue
        vlan_name = node.get("vlan", "default")
        vid = vlan_map.get(vlan_name, vlan_base)
        netinfo = net_by_name.get(vlan_name)
        count = int(node.get("count", 1) or 1)
        for r in range(count):
            suffix = f"{node.get('id', 'vm')}-{r}" if count > 1 else node.get("id", "vm")
            name = f"{range_id[:8]}-{suffix}"
            given_os = _node_os(node)
            os_alias = canonical_os(given_os)
            template_name = next(
                (t for t in map(resolve_template, os_alias_candidates(given_os)) if t), None
            )
            if template_name is None:
                unresolved.append(os_alias)
                template_name = os_alias  # best-effort; provisioning will surface the miss
            ip = node.get("ip", "")
            if not ip and netinfo:
                ip = str(ipaddress.ip_address(netinfo["next"]))
                netinfo["next"] += 1
            specs = _node_specs(node)
            gateway = netinfo["gateway"] if netinfo else ""
            netmask = str(netinfo["net"].netmask) if netinfo else "255.255.255.0"
            prefix = netinfo["net"].prefixlen if netinfo else 24
            vms.append({
                "name": name, "node_id": node.get("id", suffix), "role": node.get("role", "generic"),
                "os": os_alias, "template_name": template_name,
                "vlan_id": vid, "vlan_tag": vid,
                "ip": ip, "gateway": gateway, "netmask": netmask, "prefix": prefix,
                "cores": specs.get("cores", 2),
                "memory": specs.get("memory_mb", 4096), "memory_mb": specs.get("memory_mb", 4096),
                "disk_gb": specs.get("disk_gb", 60),
                "services": node.get("services", []),
            })

    noise_net, noise_nics = noise_mgmt_plan(template, noise_mgmt)
    if noise_net:
        networks.append(noise_net)
        # Agent VMs get a full guest network config (vSphere guestinfo), so they need
        # the range's resolvers: its domain controllers / DNS servers.
        dns = [
            str(n.get("ip")) for n in template.get("nodes") or []
            if n.get("ip") and (n.get("role") == "domain_controller"
                                or "dns" in {str(s).lower() for s in n.get("services") or []})
        ]
        domain = str(_noise_block(template).get("domain") or "corp.local")
        for vm in vms:
            host = vm["name"][len(range_id[:8]) + 1:]
            if host in noise_nics:
                vm["mgmt"] = noise_nics[host]
                if dns:
                    vm["dns"] = dns
                vm["dns_search"] = [domain]

    return {
        "range_id": range_id, "range_name": range_name,
        "vm_definitions": vms, "network_definitions": networks,
        "vlan_map": vlan_map, "unresolved": sorted(set(unresolved)),
    }


def golden_image_resolver(db, hypervisor: str = "vsphere") -> Callable[[str], str | None]:
    """Build an os-alias -> template_name resolver from the golden_images table (raw SQL)."""
    import json as _json

    from sqlalchemy import text

    rows = db.execute(
        text(
            "SELECT catalogue_id, template_name, os_aliases FROM golden_images "
            "WHERE hypervisor = :h AND enabled = TRUE AND deleted_at IS NULL"
        ),
        {"h": hypervisor},
    ).fetchall()
    by_id: dict[str, str] = {}
    by_alias: dict[str, str] = {}
    for cid, tname, aliases_json in rows:
        tn = tname or cid
        by_id[cid] = tn
        try:
            for a in _json.loads(aliases_json or "[]"):
                by_alias.setdefault(a, tn)
        except (ValueError, TypeError):
            pass

    def _resolve(os_alias: str) -> str | None:
        if not os_alias:
            return None
        return by_id.get(os_alias) or by_alias.get(os_alias)

    return _resolve


def load_template(raw: str | None) -> dict:
    """A template's stored text as a dict: YAML (content/ranges/*.yaml), or JSON. Empty or
    unreadable gives {}: provisioners do not require template content."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        try:
            import yaml

            return yaml.safe_load(raw) or {}
        except Exception:  # noqa: BLE001
            return {}
