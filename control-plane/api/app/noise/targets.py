"""What an agent may be pointed at.

Agents are dual-homed (training NIC + management NIC), so a target is a way to make them
connect somewhere. Reject anything that is not a plain host, address or small subnet,
and anything in the address families that only ever mean "not the range": loopback,
link-local (cloud metadata lives there), multicast, unspecified.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

MAX_TARGETS_PER_POOL = 64
MAX_TARGET_LEN = 253
MIN_SUBNET_PREFIX = 24  # an "inventory scan" covers at most a /24

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def _bad_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified


def _check_host(host: str) -> str | None:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        if host.lower() == "localhost" or not _HOSTNAME.match(host):
            return "not a hostname or IP address"
        return None
    return "loopback/link-local/multicast/unspecified address" if _bad_address(addr) else None


def problem(pool: str, value: str) -> str | None:
    """Why ``value`` is not acceptable in ``pool``, or None if it is."""
    if not value or len(value) > MAX_TARGET_LEN:
        return f"must be 1-{MAX_TARGET_LEN} characters"
    if pool == "subnet":
        try:
            net = ipaddress.ip_network(value, strict=False)
        except ValueError:
            return "not a CIDR"
        if net.version != 4 or net.prefixlen < MIN_SUBNET_PREFIX:
            return f"subnet must be IPv4 /{MIN_SUBNET_PREFIX} or smaller"
        return "loopback/link-local/multicast/unspecified network" if _bad_address(net.network_address) else None
    if pool == "web" and "://" in value:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https"):
            return "only http/https URLs"
        if parts.username or parts.password or not parts.hostname:
            return "URL must have a host and no credentials"
        return _check_host(parts.hostname)
    host, _, port = value.rpartition(":") if value.count(":") == 1 else (value, "", "")
    if port and not port.isdigit():
        return "bad port"
    return _check_host(host or value)


def validate(targets: dict[str, list[str]], pools: set[str]) -> list[str]:
    """Every problem with a targets mapping, as human-readable strings."""
    errors = [f"unknown target pool {p!r}" for p in sorted(set(targets) - pools)]
    for pool, values in sorted(targets.items()):
        if len(values) > MAX_TARGETS_PER_POOL:
            errors.append(f"{pool}: at most {MAX_TARGETS_PER_POOL} targets")
            continue
        for v in values:
            if why := problem(pool, v):
                errors.append(f"{pool}: {v!r}: {why}")
    return errors
