"""Noise agents' management addresses: reserved per range on the shared portgroup.

Every noise-enabled range puts its agents on one management portgroup (vSphere
``VSPHERE_NOISE_NETWORK``, VLAN ``noise.mgmt.vlan_id``), so an address must be unique
across all ranges and tenants on it, not just within a template. ``reserve`` runs when a
provision is accepted (app/range_ops.py), in the same transaction; the worker is handed
the result and builds each agent's NIC at that address; deploy registers the agent at
the same address. Nothing derives an address from template order any more.

Reservations are released when the range is destroyed (``range_ops.reconcile``).
"""

from __future__ import annotations

import ipaddress
import uuid

import yaml
from sqlalchemy.orm import Session

from .. import network_inventory
from ..models import Range
from ..models_network import NetworkReservation
from . import topology

KIND = "noise_mgmt_ip"


def settings(template: dict) -> dict:
    """The template's management network, defaults filled in."""
    return {**topology.DEFAULT_MGMT, **(topology.noise_block(template).get("mgmt") or {})}


def domain(template: dict) -> str:
    """The shared network these addresses must be unique in: one portgroup per VLAN."""
    mgmt = settings(template)
    net = ipaddress.ip_network(str(mgmt["cidr"]), strict=False)
    return f"noise-mgmt:vlan{int(mgmt['vlan_id'])}:{net}"


def pool(template: dict) -> list[str]:
    """Agent addresses: .1 up to .{MGMT_FIRST_HOST - 1} are left for the controller side."""
    net = ipaddress.ip_network(str(settings(template)["cidr"]), strict=False)
    first = int(net.network_address) + topology.MGMT_FIRST_HOST
    return [str(a) for a in net.hosts() if int(a) >= first]


def range_template(rng: Range) -> dict:
    """The range's template as a dict; {} when it is missing or not valid YAML (the
    worker reports that when it provisions; it has no noise either way)."""
    try:
        loaded = yaml.safe_load(rng.template.yaml if rng.template else "") or {}
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def reserve(db: Session, rng: Range, template: dict) -> dict[str, str]:
    """{agent node: address} for every agent node, reserving as needed. Does not commit.

    Empty when the template has no noise. Raises ``network_inventory.PoolExhaustedError``
    when the management network cannot hold this range's agents.
    """
    nodes = [n["node"] for n in topology.agent_nodes(template)]
    if not nodes:
        return {}
    return network_inventory.reserve(db, rng, domain=domain(template), kind=KIND, pool=pool(template), holders=nodes)


def reserved(db: Session, range_id: uuid.UUID) -> dict[str, str]:
    """What the range holds now: {agent node: address}."""
    rows = db.query(NetworkReservation).filter(NetworkReservation.range_id == range_id, NetworkReservation.kind == KIND)
    return {r.holder: r.value for r in rows}
