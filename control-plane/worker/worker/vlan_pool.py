"""The physical VLAN pool for isolated ranges (VSPHERE_VLAN_POOL).

A template's VLAN ids are logical labels: two ranges built from one template both say
"VLAN 200", so each range gets its own physical VLANs out of a pool reserved for ranges
(``100-199``). This parses the pool; who holds which VLAN is the ``network_reservations``
table's business (db_ops.reserve_values), never decided here.
"""

from __future__ import annotations


def parse_pool(spec: str) -> list[int]:
    """``"100-199"`` or ``"100-149,160,170-179"`` -> sorted VLAN ids (1..4094 only)."""
    vlans: set[int] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        start, end = int(lo), int(hi or lo)
        if not (1 <= start <= end <= 4094):
            raise ValueError(f"VLAN pool entry {part!r} is outside 1-4094")
        vlans.update(range(start, end + 1))
    if not vlans:
        raise ValueError(f"VLAN pool {spec!r} is empty")
    return sorted(vlans)
