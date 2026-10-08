"""The address a request came from, believing ``X-Forwarded-For`` only from our own proxies.

Anyone can send ``X-Forwarded-For``. Taking its first entry at face value let a client
pick the address that auth-zone IP allowlists, the rate limiter and the noise agents'
``NOISE_AGENT_CIDRS`` check saw. Now the header counts only when the immediate peer is a
trusted proxy (``TRUSTED_PROXY_CIDRS``), and then the chain is read right to left: the
first address that is not itself a trusted proxy is the client. The bundled nginx
configs overwrite the header with ``$remote_addr``, so a client cannot seed it.

``TRUSTED_PROXY_CIDRS`` (comma-separated) defaults to loopback plus Docker's default
bridge pool (172.16.0.0/12), where the compose stack's nginx lives. Set it to the
proxies' real addresses in production; set it to ``none`` to ignore the header.
"""

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache

from starlette.requests import HTTPConnection

DEFAULT_TRUSTED_PROXIES = "127.0.0.0/8,::1/128,172.16.0.0/12"

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=8)
def _parse(raw: str) -> tuple[Network, ...]:
    if raw.strip().lower() in ("", "none"):
        return ()
    return tuple(ipaddress.ip_network(c.strip(), strict=False) for c in raw.split(",") if c.strip())


def trusted_proxies() -> tuple[Network, ...]:
    return _parse(os.getenv("TRUSTED_PROXY_CIDRS", DEFAULT_TRUSTED_PROXIES))


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped or addr


def _trusted(addr, proxies: tuple[Network, ...]) -> bool:
    return addr is not None and any(addr.version == n.version and addr in n for n in proxies)


def client_ip(request: HTTPConnection) -> str | None:
    """The client's address: the peer, unless the peer is a trusted proxy that forwarded."""
    peer = request.client.host if request.client else None
    proxies = trusted_proxies()
    if not proxies or not _trusted(_ip(peer or ""), proxies):
        return peer
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(hops):
        addr = _ip(hop)
        if addr is None:
            return peer  # a malformed chain is not evidence of anything
        if not _trusted(addr, proxies):
            return str(addr)
    return str(_ip(hops[0])) if hops else peer
