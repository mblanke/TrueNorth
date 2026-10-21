"""Site networks a range's own subnets may not overlap (TN_RANGE_RESERVED_CIDRS).

A range network that reuses the management, build or depot-uplink subnet would route
range traffic onto the site (an edge firewall with a WAN on the uplink network and a LAN
on the same /24 is the obvious one). The installer sets ``tn_range_reserved_cidrs``
(the work site: 10.30.30.0/24, 10.30.31.0/24, 10.30.32.0/24); empty means none.

The API refuses such a template when it is validated, created or updated
(app/network_inventory/reserved.py: the same rule, which the worker may not import);
the worker refuses it again at provision (range_alloc.reserve_for_build), because the
setting may have changed since, and a template may predate it. Range certification S1.
"""

from __future__ import annotations

import ipaddress
import os


def reserved_cidrs(spec: str | None = None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """The reserved networks: ``spec``, else TN_RANGE_RESERVED_CIDRS (comma separated)."""
    raw = os.getenv("TN_RANGE_RESERVED_CIDRS", "") if spec is None else spec
    out = []
    for part in raw.replace(";", ",").split(","):
        if part := part.strip():
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError as exc:
                raise ValueError(f"TN_RANGE_RESERVED_CIDRS entry {part!r} is not a network ({exc})") from exc
    return out


def check_settings() -> None:
    """Refuse to start on a malformed TN_RANGE_RESERVED_CIDRS or VSPHERE_RESERVED_VLANS
    (celery_app, at import): every build would otherwise fail on it."""
    from .vlan_pool import parse_reserved

    reserved_cidrs()
    raw = os.getenv("VSPHERE_RESERVED_VLANS", "")
    try:
        parse_reserved(raw)
    except ValueError as exc:
        raise ValueError(f"VSPHERE_RESERVED_VLANS={raw!r} is invalid: {exc}") from exc


def template_cidrs(template: dict) -> list[tuple[str, str]]:
    """(where, cidr) of every subnet a template declares; of a rendered one (it has
    ``networks``, render.py's network_definitions), those only."""
    out: list[tuple[str, str]] = []
    for i, n in enumerate(template.get("networks") or []):
        if isinstance(n, dict):
            for key in ("cidr", "subnet"):
                if n.get(key):
                    out.append((f"networks[{i}] {n.get('name') or ''}".strip(), str(n[key])))
    if out:
        return out
    net = template.get("network")
    if isinstance(net, dict):
        for i, v in enumerate(net.get("vlans") or []):
            if isinstance(v, dict) and v.get("cidr"):
                out.append((f"network.vlans[{i}] {v.get('name') or ''}".strip(), str(v["cidr"])))
    for key, val in (template.get("inputs") or {}).items() if isinstance(template.get("inputs"), dict) else ():
        if str(key).startswith("cidr_") and val:
            out.append((f"inputs.{key}", str(val)))
    return out


def conflicts(template: dict, reserved=None) -> list[str]:
    """One message per template subnet that overlaps a reserved network."""
    reserved = reserved_cidrs() if reserved is None else reserved
    if not reserved:
        return []
    found = []
    for where, cidr in template_cidrs(template):
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue  # not a network: the schema validation reports it
        for site in reserved:
            if net.version == site.version and net.overlaps(site):
                found.append(f"{where}: {net} overlaps the reserved site network {site} (TN_RANGE_RESERVED_CIDRS)")
    return list(dict.fromkeys(found))
