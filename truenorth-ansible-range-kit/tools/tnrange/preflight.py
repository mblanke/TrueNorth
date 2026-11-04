"""Live VLAN collision check used by the range_preflight role.

Input is the raw `dvs_portgroup_info` dict returned by community.vmware.vmware_dvs_portgroup_info
(show_vlan_info: true): {dvs_name: [{portgroup_name, vlan_info: {trunk, pvlan, vlan_id}}]}.
vlan_id is a string ("2000") or, for trunks, a list of strings ("100-200").
"""

from __future__ import annotations

import re

from .allocate import expand_vlans
from .common import RangeError

DEFAULT_IGNORE = [r".*-DVUplinks-\d+$"]   # uplink portgroups trunk everything by design


def check(plan: dict, pg_info: dict, ignore: list[str] | None = None) -> list[str]:
    ignore_re = [re.compile(p) for p in (ignore if ignore is not None else DEFAULT_IGNORE)]
    planned = {n["portgroup"]: n["vlan"] for i in plan["instances"] for n in i["networks"]}
    ours = set(planned.values())
    errs: list[str] = []
    for dvs, pgs in (pg_info or {}).items():
        for pg in pgs or []:
            name = pg.get("portgroup_name", "")
            vi = pg.get("vlan_info") or {}
            try:
                vlans = set(expand_vlans(vi.get("vlan_id")))
            except ValueError:
                errs.append(f"{dvs}/{name}: unparseable VLAN '{vi.get('vlan_id')}'")
                continue
            if name in planned:
                if vi.get("trunk") or vlans != {planned[name]}:
                    errs.append(f"{dvs}/{name} exists with VLAN {sorted(vlans)} (trunk={vi.get('trunk')}); "
                                f"plan expects {planned[name]}")
                continue
            if any(r.match(name) for r in ignore_re):
                continue
            hit = sorted(vlans & ours)
            if hit:
                kind = "trunk" if vi.get("trunk") else "portgroup"
                errs.append(f"{dvs}/{name} ({kind}) already uses range VLAN(s) {hit}")
    return errs


def check_or_raise(plan: dict, pg_info: dict, ignore: list[str] | None = None) -> None:
    errs = check(plan, pg_info, ignore)
    if errs:
        raise RangeError(["VLAN collision(s) with live portgroups; nothing was changed:"] + errs)
