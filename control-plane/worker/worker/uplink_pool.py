"""WAN addresses for ranges' edge firewalls on the depot uplink network.

With VSPHERE_RANGE_UPLINK_NETWORK set (lab: ``dPG-TN-SVC``), each range's edge firewall
gets a WAN NIC there with a static address of its own out of VSPHERE_RANGE_UPLINK_POOL
(``10.30.32.100-10.30.32.199``). Pure functions, like vlan_pool: the caller supplies the
addresses other ranges hold and persists the result in provisioner_output["uplink"].
"""

from __future__ import annotations

import ipaddress


class UplinkPoolExhaustedError(RuntimeError):
    """Every address in the uplink pool is held by another range."""


def parse_ip_pool(spec: str) -> list[str]:
    """``"10.30.32.100-10.30.32.199"``, ``"10.30.32.100-199"`` or a comma list -> addresses."""
    out: list[ipaddress.IPv4Address] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        start = ipaddress.IPv4Address(lo.strip())
        if not hi:
            end = start
        elif "." in hi:
            end = ipaddress.IPv4Address(hi.strip())
        else:  # "10.30.32.100-199": the last octet only
            end = ipaddress.IPv4Address(f"{lo.strip().rsplit('.', 1)[0]}.{hi.strip()}")
        if end < start:
            raise ValueError(f"uplink pool entry {part!r} runs backwards")
        out.extend(ipaddress.IPv4Address(i) for i in range(int(start), int(end) + 1))
    if not out:
        raise ValueError(f"uplink pool {spec!r} is empty: set VSPHERE_RANGE_UPLINK_POOL")
    return [str(a) for a in sorted(set(out))]


def allocate_ip(used: set[str], pool: list[str]) -> str:
    """The lowest pool address no other range holds."""
    for addr in pool:
        if addr not in used:
            return addr
    raise UplinkPoolExhaustedError(f"all {len(pool)} addresses in the range uplink pool are in use")
