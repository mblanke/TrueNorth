"""Read a range template and work out where noise lives and what it talks to.

Template opt-in, all optional except ``enabled``::

    noise:
      enabled: true
      preset: office            # or level: 0-100
      seed: 7
      domain: corp.local
      personas: 30
      exclude_vlans: [red_team] # default: anything named like red/blue/soc/mgmt/management
      mgmt: {vlan_id: 4001, cidr: 10.255.0.0/24}

    nodes:
      - id: ws01
        role: workstation
        noise: {agent: false}   # opt a node out (or in, for any other role)

Agents go on workstations and user-simulation boxes by default. Which nodes are agent
nodes, and the order their management addresses are handed out in, MUST match
``control-plane/worker/worker/render.py`` (``noise_agent_vms``): the worker builds the
management NIC, this side registers the agent. tests/api/test_noise_topology.py fails
if the two drift apart.
"""

from __future__ import annotations

import ipaddress
import re

AGENT_ROLES = frozenset({"workstation", "usersim", "traffic_generator"})
# Never put an agent (or a target) in the attackers' enclave, on the students' own
# analyst desks, or in exercise control: the agent is out of bounds and must not sit
# where the people it is hidden from work.
DEFAULT_EXCLUDE = re.compile(r"red|blue|soc|mgmt|management", re.I)
DEFAULT_MGMT = {"vlan_id": 4001, "cidr": "10.255.0.0/24"}
MGMT_FIRST_HOST = 10  # .1 is the controller's side, .10 onward the agents

# Node role -> the target pools it serves.
ROLE_POOLS: dict[str, tuple[str, ...]] = {
    "domain_controller": ("dc", "ntp"),
    "web_server": ("web",),
    "web_application": ("web",),
    "reverse_proxy": ("web",),
    "mail_server": ("mail",),
    "mail_gateway": ("mail",),
    "file_server": ("share",),
    "usersim": ("web", "mail", "share"),  # the persona-facing intranet/mail/file box
}
SERVER_SSH_ROLES = frozenset({"backup_server", "database_server", "web_server", "web_application", "file_server"})


def noise_block(template: dict) -> dict:
    block = template.get("noise")
    return block if isinstance(block, dict) else {}


def _vlan_cidrs(template: dict) -> dict[str, str]:
    net = template.get("network") if isinstance(template.get("network"), dict) else {}
    return {str(v.get("name")): str(v.get("cidr", "")) for v in net.get("vlans") or [] if v.get("name")}


def _excluded(vlan: str, block: dict) -> bool:
    if "exclude_vlans" in block:
        return vlan in set(block.get("exclude_vlans") or [])
    return bool(DEFAULT_EXCLUDE.search(vlan or ""))


def _expand(template: dict) -> list[tuple[str, dict]]:
    """(vm hostname, node) in template order, with ``count`` expanded as render.py does."""
    out = []
    for node in template.get("nodes") or []:
        count = int(node.get("count", 1) or 1)
        nid = str(node.get("id", "vm"))
        for r in range(count):
            out.append((f"{nid}-{r}" if count > 1 else nid, node))
    return out


def is_agent_node(node: dict, block: dict) -> bool:
    opt = node.get("noise") if isinstance(node.get("noise"), dict) else {}
    if "agent" in opt:
        return bool(opt["agent"])
    return node.get("role") in AGENT_ROLES and not _excluded(str(node.get("vlan", "")), block)


def agent_nodes(template: dict) -> list[dict]:
    """Every agent node with its management address. Empty unless noise is enabled."""
    block = noise_block(template)
    if not block.get("enabled"):
        return []
    mgmt = {**DEFAULT_MGMT, **(block.get("mgmt") or {})}
    net = ipaddress.ip_network(mgmt["cidr"], strict=False)
    out = []
    for i, (name, node) in enumerate((n, nd) for n, nd in _expand(template) if is_agent_node(nd, block)):
        out.append(
            {
                "node": name,
                "zone": str(node.get("vlan", "")),
                "os": str(node.get("os", "")),
                "platform": "windows" if str(node.get("os", "")).lower().startswith("win") else "linux",
                "ip": str(node.get("ip", "")),
                "mgmt_ip": str(net.network_address + MGMT_FIRST_HOST + i),
                "mgmt_prefix": net.prefixlen,
                "mgmt_vlan": int(mgmt["vlan_id"]),
            }
        )
    return out


def derive_targets(template: dict) -> dict[str, list[str]]:
    """Target pools from the template's own servers. Excluded VLANs contribute nothing."""
    block = noise_block(template)
    domain = str(block.get("domain") or "corp.local")
    pools: dict[str, list[str]] = {}

    def add(pool: str, value: str) -> None:
        if value and value not in pools.setdefault(pool, []):
            pools[pool].append(value)

    cidrs = _vlan_cidrs(template)
    for name, node in _expand(template):
        vlan = str(node.get("vlan", ""))
        if _excluded(vlan, block):
            continue
        ip = str(node.get("ip", ""))
        role = str(node.get("role", ""))
        services = {str(s).lower() for s in node.get("services") or []}
        if not ip:
            continue
        for pool in ROLE_POOLS.get(role, ()):
            add(pool, f"http://{ip}/" if pool == "web" else ip)
        is_server = role in ROLE_POOLS or role in SERVER_SSH_ROLES or ("ssh" in services and role not in AGENT_ROLES)
        if is_server:
            add("dns", f"{name}.{domain}")
        if "ssh" in services and role not in AGENT_ROLES:
            add("ssh", ip)
        if role in AGENT_ROLES:
            add("hosts", ip)
            if cidrs.get(vlan):
                add("subnet", cidrs[vlan])
    return pools
