"""Site networks a range template's subnets may not overlap (TN_RANGE_RESERVED_CIDRS).

The installer's ``tn_range_reserved_cidrs`` (the work site: its management, build and
depot-uplink /24s). A range network on one of them would put range traffic on the site.
Template validation, create and update refuse an overlap (routers/templates.py); the
worker refuses it again at provision, since the setting can change after a template was
saved (worker/reserved_networks.py: the same rule, which this package may not import).
Range certification S1.
"""

from __future__ import annotations

import ipaddress
import os


def reserved_cidrs(spec: str | None = None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """``spec``, else TN_RANGE_RESERVED_CIDRS: comma-separated networks. Empty: none."""
    raw = os.getenv("TN_RANGE_RESERVED_CIDRS", "") if spec is None else spec
    return [ipaddress.ip_network(p.strip(), strict=False) for p in raw.replace(";", ",").split(",") if p.strip()]


def _declared(doc: dict) -> list[tuple[str, str]]:
    """(path, cidr) of every subnet the template declares."""
    out: list[tuple[str, str]] = []
    net = doc.get("network")
    if isinstance(net, dict):
        for i, v in enumerate(net.get("vlans") or []):
            if isinstance(v, dict) and v.get("cidr"):
                out.append((f"network.vlans.{i}.cidr", str(v["cidr"])))
    inputs = doc.get("inputs")
    if isinstance(inputs, dict):
        out += [(f"inputs.{k}", str(v)) for k, v in inputs.items() if str(k).startswith("cidr_") and v]
    for i, n in enumerate(doc.get("networks") or []):
        if isinstance(n, dict):
            out += [(f"networks.{i}.{k}", str(n[k])) for k in ("cidr", "subnet") if n.get(k)]
    return out


def template_conflicts(doc, reserved=None) -> list[dict[str, str]]:
    """``{path, message}`` for each template subnet overlapping a reserved site network."""
    if not isinstance(doc, dict):
        return []
    reserved = reserved_cidrs() if reserved is None else reserved
    if not reserved:
        return []
    errors = []
    for path, cidr in _declared(doc):
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue  # the schema reports a malformed CIDR
        for site in reserved:
            if net.version == site.version and net.overlaps(site):
                errors.append({"path": path, "message": f"{net} overlaps the reserved site network {site} "
                                                        "(tn_range_reserved_cidrs); choose another subnet"})
    return errors
