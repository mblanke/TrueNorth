"""Physical VLAN allocation for isolated ranges.

A template's VLAN ids are logical labels: two ranges built from one template both say
"VLAN 200", so each range gets its own physical VLANs out of a pool reserved for ranges
(VSPHERE_VLAN_POOL, e.g. ``100-199``). Pure functions; the caller supplies what other
ranges already hold and persists the result.
"""

from __future__ import annotations


class VlanPoolExhaustedError(RuntimeError):
    """Not enough free VLANs in the pool for this range."""


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


def allocate(logical_vlans, used: set[int], pool: list[int]) -> dict[int, int]:
    """Map each logical VLAN to a free physical VLAN from ``pool``, lowest first.

    ``used`` holds the physical VLANs other ranges own. Raises VlanPoolExhaustedError
    rather than hand out a VLAN twice: two ranges on one VLAN are not isolated.
    """
    wanted = sorted({int(v) for v in logical_vlans})
    free = [v for v in pool if v not in used]
    if len(free) < len(wanted):
        raise VlanPoolExhaustedError(
            f"range needs {len(wanted)} VLANs but only {len(free)} of the {len(pool)} in the pool are free"
        )
    return dict(zip(wanted, free, strict=False))
