"""Noise agents' management addresses: reserved per range on the shared portgroup.

Every noise-enabled range puts its agents on one management portgroup (vSphere
``VSPHERE_NOISE_NETWORK``, VLAN ``noise.mgmt.vlan_id``), so an address must be unique
across all ranges and tenants on it, not just within a template. ``reserve`` runs when a
provision is accepted (app/range_ops/service.py), in the same transaction; the worker is handed
the result and builds each agent's NIC at that address; deploy registers the agent at
the same address. Nothing derives an address from template order any more.

Reservations are released when the range is destroyed (``range_ops.service.reconcile``).
"""

from __future__ import annotations

import ipaddress
import uuid

import yaml
from sqlalchemy.orm import Session

from .. import network_inventory, safe_yaml
from ..models import Range
from ..network_inventory import NetworkReservation
from . import topology

KIND = "noise_mgmt_ip"


def settings(template: dict) -> dict:
    """The template's management network, defaults filled in."""
    return {**topology.DEFAULT_MGMT, **(topology.noise_block(template).get("mgmt") or {})}


def domain(template: dict) -> str:
    """The shared network these addresses must be unique in: the management portgroup,
    one per VLAN. Not the CIDR: a /24 and a /25 of it are the same wire."""
    return f"noise-mgmt:vlan{int(settings(template)['vlan_id'])}"


def pool(template: dict) -> list[str]:
    """Agent addresses: .1 up to .{MGMT_FIRST_HOST - 1} are left for the controller side."""
    net = ipaddress.ip_network(str(settings(template)["cidr"]), strict=False)
    first = int(net.network_address) + topology.MGMT_FIRST_HOST
    return [str(a) for a in net.hosts() if int(a) >= first]


def range_template(rng: Range) -> dict:
    """The range's template as a dict; {} when it is missing or not valid YAML (the
    worker reports that when it provisions; it has no noise either way)."""
    try:
        loaded = safe_yaml.load(rng.template.yaml if rng.template else "") or {}
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def reserve(db: Session, rng: Range, template: dict) -> dict[str, str]:
    """{agent node: address} for every agent node, reserving as needed and releasing what
    the range no longer needs. Does not commit.

    Empty (and nothing held) when the template has no noise. Raises ``network_inventory.PoolExhaustedError``
    when the management network cannot hold this range's agents.
    """
    nodes = [n["node"] for n in topology.agent_nodes(template)]
    # sync, not reserve: after a template edit the range releases addresses its former
    # agents held, or that are on its former management network; with noise off, all.
    return network_inventory.sync(
        db, rng, domain=domain(template), kind=KIND, pool=pool(template) if nodes else [], holders=nodes
    )


def reserved(db: Session, range_id: uuid.UUID) -> dict[str, str]:
    """What the range holds now: {agent node: address}."""
    rows = db.query(NetworkReservation).filter(NetworkReservation.range_id == range_id, NetworkReservation.kind == KIND)
    return {r.holder: r.value for r in rows}
