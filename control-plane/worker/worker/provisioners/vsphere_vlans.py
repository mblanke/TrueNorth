"""TrueNorth Range - which VLANs the live range switch already carries (range certification S1).

Two checks use it, both against what vCenter says now, not against what TrueNorth recorded:

* **Before allocation** (``VsphereAPIProvisioner.live_usage``): every port group on the
  range vDS (or the hosts' standard vSwitches), trunks expanded, uplink port groups
  skipped. VLANs used outside TrueNorth become external reservations, so the allocator
  never hands them out (worker/range_alloc.record_external).
* **Before each port group is created** (``_prepare_sync``): ``conflict`` says whether
  the VLAN, or the port group's name, appeared since the allocation. The build then
  stops: nothing further is created and what this step created is removed.

Ported from the Ansible range kit's live preflight (tools/tnrange/preflight.py): uplink
port groups (``*-DVUplinks-<n>``) trunk every VLAN by design and are ignored; a port
group with the planned name but another VLAN, or a trunk, is a collision; any other port
group carrying the VLAN, trunked or not, is a collision.

TrueNorth's own range port groups are ``tn-<range8>-v<vlan>`` (vsphere_infra.portgroup_name)
and, since S1, carry the description ``TrueNorth range <range id>`` (``owner_mark``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UPLINK = re.compile(r".*-DVUplinks-\d+$")
RANGE_PORTGROUP = re.compile(r"^tn-[0-9a-f]{8}-v\d+$")
OWNER = "TrueNorth range "
VSS_ALL_VLANS = 4095  # a standard port group's "trunk everything"


class PortgroupConflictError(RuntimeError):
    """A port group the build was about to create collides with the live switch."""

    def __init__(self, message: str, vlan: int, used_by: str):
        super().__init__(message)
        self.vlan, self.used_by = vlan, used_by


@dataclass(frozen=True)
class PortgroupUse:
    """One port group on the switch and the VLANs it carries."""

    name: str
    vlans: frozenset[int]
    trunk: bool = False
    description: str = ""

    @property
    def managed(self) -> bool:
        """TrueNorth's own range port group (by name or owner mark)."""
        return bool(RANGE_PORTGROUP.match(self.name)) or self.description.startswith(OWNER)


def owner_mark(range_id: str) -> str:
    return f"{OWNER}{range_id}"


def expand(spec) -> tuple[frozenset[int], bool]:
    """(VLANs, trunk?) of a vDS port setting: a VLAN id (0: none), a trunk (a list of
    numeric ranges), or a private VLAN (its secondary id)."""
    if spec is None:
        return frozenset(), False
    pvlan = getattr(spec, "pvlanId", None)
    if isinstance(pvlan, int):
        return (frozenset({pvlan}) if 1 <= pvlan <= 4094 else frozenset()), False
    vlan = getattr(spec, "vlanId", spec)
    if isinstance(vlan, int):
        return (frozenset({vlan}) if 1 <= vlan <= 4094 else frozenset()), False
    out: set[int] = set()
    for part in vlan or []:
        start, end = getattr(part, "start", None), getattr(part, "end", None)
        if isinstance(start, int) and isinstance(end, int):
            out.update(range(max(start, 1), min(end, 4094) + 1))
    return frozenset(out), True


def _is_uplink(pg) -> bool:
    config = getattr(pg, "config", None)
    if getattr(config, "uplink", False) is True:
        return True
    if any(getattr(t, "key", "") == "SYSTEM/DVS.UPLINKPG" for t in getattr(pg, "tag", None) or []):
        return True
    return bool(UPLINK.match(getattr(pg, "name", "") or ""))


def dvs_usage(dvs) -> list[PortgroupUse]:
    """Every port group on ``dvs`` but its uplink port groups, read live."""
    out = []
    for pg in dvs.portgroup:
        if _is_uplink(pg):
            continue
        config = pg.config
        vlans, trunk = expand(getattr(getattr(config, "defaultPortConfig", None), "vlan", None))
        out.append(PortgroupUse(pg.name, vlans, trunk, str(getattr(config, "description", None) or "")))
    return out


def vss_usage(hosts) -> list[PortgroupUse]:
    """Every standard port group on ``hosts`` (one entry per name and VLAN), read live."""
    seen: dict[tuple[str, int], PortgroupUse] = {}
    for host in hosts:
        network = getattr(getattr(host, "config", None), "network", None)
        for pg in getattr(network, "portgroup", None) or []:
            name, vlan = pg.spec.name, int(pg.spec.vlanId or 0)
            if vlan == VSS_ALL_VLANS:
                use = PortgroupUse(name, frozenset(range(1, 4095)), True)
            else:
                use = PortgroupUse(name, frozenset({vlan}) if 1 <= vlan <= 4094 else frozenset())
            seen.setdefault((name, vlan), use)
    return list(seen.values())


def by_value(uses: list[PortgroupUse], pool) -> tuple[dict[str, str], dict[str, str]]:
    """({vlan: port group} used outside TrueNorth, {vlan: port group} TrueNorth's own),
    restricted to ``pool`` (VLAN ids as text). The first port group by name wins."""
    wanted = {str(v) for v in pool}
    foreign: dict[str, str] = {}
    managed: dict[str, str] = {}
    for use in sorted(uses, key=lambda u: u.name):
        target = managed if use.managed else foreign
        for vlan in sorted(use.vlans):
            if str(vlan) in wanted:
                target.setdefault(str(vlan), use.name)
    return foreign, managed


def conflict(uses: list[PortgroupUse], name: str, vlan: int, range_id: str, switch: str) -> str | None:
    """Why ``name`` on ``vlan`` cannot be created (or reused) for ``range_id`` now; None if
    it can. A port group of that name is reused only when it is this range's: the same
    single VLAN and no description, or this range's owner mark."""
    for use in uses:
        if use.name != name:
            continue
        mine = not use.description or use.description == owner_mark(range_id)
        if use.trunk or use.vlans != {vlan} or not mine:
            carried = ", ".join(str(v) for v in sorted(use.vlans)[:8]) or "none"
            owner = "" if mine else f", owned by {use.description!r}"
            return (f"port group {name!r} already exists on {switch} with VLAN {carried}"
                    f"{' (trunk)' if use.trunk else ''}{owner}, not this range's VLAN {vlan}"
                    if use.vlans != {vlan} or use.trunk else
                    f"port group {name!r} already exists on {switch}{owner}: it is not this range's")
    for use in sorted(uses, key=lambda u: u.name):
        if use.name != name and vlan in use.vlans:
            what = "trunk port group" if use.trunk else "port group"
            return f"VLAN {vlan} is already used by {what} {use.name!r} on {switch}"
    return None


def check(uses: list[PortgroupUse], name: str, vlan: int, range_id: str, switch: str) -> bool:
    """Raise PortgroupConflictError for ``conflict``; else whether the port group already exists."""
    if reason := conflict(uses, name, vlan, range_id, switch):
        used_by = next((u.name for u in uses if u.name == name or vlan in u.vlans), name)
        raise PortgroupConflictError(f"{reason}; it appeared after VLAN {vlan} was allocated, so the build stopped "
                                "before creating anything further", vlan, used_by)
    return any(u.name == name for u in uses)
