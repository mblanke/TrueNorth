"""Deterministic VLAN, CIDR and host address allocation.

Each range instance owns one slot. Slot k = VLAN block k of the pool and CIDR block k
of the pool. A slot is skipped if any VLAN in its block is reserved or any part of its
CIDR block overlaps a reserved network. Slot assignments are recorded in
ranges/.allocations.json so rebuilding a definition always yields the same addressing.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any

from .common import RangeError, load_json, write_json

REGISTRY = Path("ranges") / ".allocations.json"
LIVE_NETWORK = Path("discovery") / "live-network.json"


def _slot(site: dict, k: int):
    pool = site["vlan_pool"]
    per = site["vlans_per_slot"]
    vstart = pool["start"] + k * per
    vend = vstart + per - 1
    if vend > pool["end"]:
        return None
    net = ipaddress.ip_network(site["cidr_pool"])
    blocks = 2 ** (site["slot_prefix"] - net.prefixlen)
    if k >= blocks:
        return None
    size = 2 ** (net.max_prefixlen - site["slot_prefix"])
    block = ipaddress.ip_network((int(net.network_address) + k * size, site["slot_prefix"]))
    return list(range(vstart, vend + 1)), block


def max_slots(site: dict) -> int:
    pool = site["vlan_pool"]
    by_vlan = (pool["end"] - pool["start"] + 1) // site["vlans_per_slot"]
    net = ipaddress.ip_network(site["cidr_pool"])
    by_cidr = 2 ** (site["slot_prefix"] - net.prefixlen)
    return min(by_vlan, by_cidr)


def slot_conflicts(site: dict, k: int, live: dict, owner: str | None = None) -> list[str]:
    s = _slot(site, k)
    if s is None:
        return [f"slot {k} is outside the pool"]
    vlans, block = s
    out = []
    reserved_v = set(site.get("reserved_vlans", []) or [])
    hit = sorted(reserved_v.intersection(vlans))
    if hit:
        out.append(f"slot {k} VLANs include reserved {hit}")
    for c in site.get("reserved_cidrs", []) or []:
        if block.overlaps(ipaddress.ip_network(c)):
            out.append(f"slot {k} block {block} overlaps reserved {c}")
    ignore = [re.compile(x) for x in (site.get("vlan_check_ignore_portgroups")
                                     if site.get("vlan_check_ignore_portgroups") is not None
                                     else [r".*-DVUplinks-\d+$"])]
    for item in live.get("vlans_in_use", []) or []:
        pg = item.get("portgroup", "")
        if any(r.match(pg) for r in ignore):
            continue
        used = set(expand_vlans(item.get("vlans", item.get("vlan"))))
        own = owner is not None and re.fullmatch(rf"TN-{re.escape(owner)}-[a-z][a-z0-9-]*", pg) \
            and not item.get("trunk")
        hit = sorted(used.intersection(vlans))
        if hit and not own:
            out.append(f"slot {k} VLAN(s) {hit} already used by portgroup '{pg}'")
    for c in live.get("cidrs_in_use", []) or []:
        try:
            if block.overlaps(ipaddress.ip_network(c, strict=False)):
                out.append(f"slot {k} block {block} overlaps in-use network {c}")
        except ValueError:
            continue
    return out


def expand_vlans(value) -> list[int]:
    """Accept 2000, "2000", "100-200", "1-5, 9", or a list of those; return VLAN ints."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[int] = []
    for it in items:
        for part in str(it).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                out.extend(range(int(a), int(b) + 1))
            else:
                out.append(int(part))
    return out


def load_registry(root: Path) -> dict:
    p = root / REGISTRY
    if not p.exists():
        return {"version": 1, "slots": {}}
    reg = load_json(p)
    reg.setdefault("slots", {})
    return reg


def save_registry(root: Path, reg: dict) -> None:
    write_json(root / REGISTRY, reg)


def load_live(root: Path) -> dict:
    p = root / LIVE_NETWORK
    return load_json(p) if p.exists() else {}


def assign_slots(root: Path, site: dict, ids: list[str], write: bool) -> tuple[dict[str, int], list[str], dict | None]:
    """Return ({instance_id: slot}, warnings, registry_to_save_or_None). Raises on conflicts.
    The caller saves the registry only after the whole compile succeeds."""
    reg = load_registry(root)
    live = load_live(root)
    used = {v: k for k, v in reg["slots"].items()}
    result: dict[str, int] = {}
    warnings: list[str] = []
    errs: list[str] = []
    for iid in ids:
        if iid in reg["slots"]:
            k = reg["slots"][iid]
            c = slot_conflicts(site, k, live, owner=iid)
            if c:
                errs.extend(f"{iid}: recorded {x}" for x in c)
            result[iid] = k
            continue
        k = 0
        limit = max_slots(site)
        while k < limit and (k in used or slot_conflicts(site, k, live)):
            k += 1
        if k >= limit:
            errs.append(f"{iid}: no free slot left in the pool ({limit} slots)")
            continue
        used[k] = iid
        result[iid] = k
        if write:
            reg["slots"][iid] = k
        else:
            warnings.append(f"{iid}: slot {k} is proposed, not recorded (compile with --write-allocation)")
    if errs:
        raise RangeError(errs)
    if not live:
        warnings.append("discovery/live-network.json not found: VLAN collisions with non-TrueNorth "
                        "portgroups were not checked (run `tools/range discover`)")
    if not (site.get("reserved_cidrs") or live.get("cidrs_in_use")):
        warnings.append("no reserved_cidrs configured: CIDR overlap with existing networks is not checked")
    return result, warnings, (reg if write else None)


def address_instance(site: dict, resolved: dict, iid: str, k: int) -> dict[str, Any]:
    """Return networks and VMs for one instance with VLANs, CIDRs and IPs filled in."""
    vlans, block = _slot(site, k)
    nets = resolved["networks"]
    subnets = list(block.subnets(new_prefix=site["network_prefix"]))
    if len(nets) > min(len(vlans), len(subnets)):
        raise RangeError(f"{iid}: {len(nets)} networks exceed the slot size "
                         f"({min(len(vlans), len(subnets))})")
    out_nets = []
    by_name = {}
    for i, n in enumerate(nets):
        sn = subnets[i]
        entry = {
            "name": n["name"],
            "portgroup": f"TN-{iid}-{n['name']}",
            "vlan": vlans[i],
            "cidr": str(sn),
            "netmask": str(sn.netmask),
            "gateway": str(sn.network_address + 1) if n["gateway"] else None,
            "security": n["security"],
            "source": n["source"],
            "_used": set([1]) if n["gateway"] else set(),
            "_net": sn,
        }
        out_nets.append(entry)
        by_name[n["name"]] = entry

    errs: list[str] = []
    # Explicit hosts first so auto-assignment never takes them.
    for vm in resolved["vms"]:
        for nic in vm["nics"]:
            h = nic["host"]
            if h == "auto":
                continue
            net = by_name[nic["network"]]
            if h == 1 and net["gateway"]:
                if h in net.get("_gw_taken", set()):
                    errs.append(f"{iid}: two VMs claim gateway .1 on {nic['network']}")
                net.setdefault("_gw_taken", set()).add(h)
                continue
            if h in net["_used"]:
                errs.append(f"{iid}: host .{h} on {nic['network']} is claimed twice")
            net["_used"].add(h)

    out_vms = []
    for vm in resolved["vms"]:
        nics = []
        default_route_taken = False
        for nic in vm["nics"]:
            net = by_name[nic["network"]]
            h = nic["host"]
            if h == "auto":
                h = site.get("first_host_offset", 10)
                while h in net["_used"]:
                    h += 1
                net["_used"].add(h)
            sn = net["_net"]
            if h >= sn.num_addresses - 1:
                errs.append(f"{iid}: {nic['network']} has run out of host addresses")
                continue
            ip = str(sn.network_address + h)
            is_gw = net["gateway"] is not None and ip == net["gateway"]
            gw = None if is_gw or default_route_taken else net["gateway"]
            if gw:
                default_route_taken = True  # one default route per VM, on its first routed NIC
            nics.append({
                "network": nic["network"],
                "portgroup": net["portgroup"],
                "ip": ip,
                "netmask": net["netmask"],
                "prefix": sn.prefixlen,
                "gateway": gw,
            })
        out_vms.append({
            "name": vm["name"],
            "vm_name": f"{iid}-{vm['name']}",
            "role": vm["role"],
            "template_alias": vm["template"],
            "cpu": vm["cpu"],
            "memory_mb": vm["memory_gb"] * 1024,
            "disk_gb": vm["disk_gb"],
            "guest_config": vm["guest_config"],
            "nics": nics,
            "vars": vm.get("vars", {}),
            "source": vm["source"],
        })
    if errs:
        raise RangeError(errs)

    for n in out_nets:
        gw_claimed = 1 in n.get("_gw_taken", set())
        n["has_router"] = gw_claimed
        for k_ in ("_used", "_net", "_gw_taken"):
            n.pop(k_, None)
    return {"networks": out_nets, "vms": out_vms, "vlans": vlans[: len(out_nets)], "block": str(block)}
