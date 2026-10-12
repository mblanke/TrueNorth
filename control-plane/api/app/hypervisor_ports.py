"""The port a hypervisor connection gets when none is given, by family.

Until 2026-10-11 every connection defaulted to 8006 (Proxmox's API port), so a vSphere
connection created without a port was stored as 8006. vCenter answers on 443, Hyper-V's
WinRM on 5985. No imports from the app: schemas and models may use this.
"""

from __future__ import annotations

DEFAULT_PORTS: dict[str, int] = {"vsphere": 443, "proxmox": 8006, "hyperv": 5985}


def default_port(hypervisor_type: str | None) -> int:
    return DEFAULT_PORTS.get(hypervisor_type or "", 8006)


def endpoint_url(host: str, port=None) -> str:
    """``https://host[:port]`` of a vSphere connection, as the worker builds it
    (worker/provisioners/vsphere_api.endpoint_url; tests/contracts keeps the two equal).
    No port for 443, nor for 8006 (the old default for every family); a port in ``host``
    wins; a bare IPv6 address is bracketed. ValueError for an unusable address."""
    from urllib.parse import urlparse

    host = str(host or "").strip()
    if "://" not in host:
        if host.count(":") >= 2 and not host.startswith("["):
            host = f"[{host}]"
        host = f"https://{host}"
    url = host.rstrip("/")
    parsed = urlparse(url)
    try:
        explicit = parsed.port
    except ValueError as exc:
        raise ValueError(f"not a usable vCenter address: {url!r}") from exc
    if not parsed.hostname:
        raise ValueError(f"not a usable vCenter address: {url!r}")
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        port = 0
    if port in (0, 443, 8006) or explicit is not None:
        return url
    return parsed._replace(netloc=f"{parsed.netloc}:{port}").geturl()


def endpoint_key(url: str) -> tuple[str, int]:
    """(host, port) of an endpoint URL, for comparing two spellings of one vCenter."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url if "://" in str(url) else endpoint_url(url))
        return (parsed.hostname or "").lower(), parsed.port or 443
    except ValueError:
        return str(url), 0


def column_default(context) -> int:
    """SQLAlchemy column default (models.HypervisorConnection.port): the row's family's port."""
    return default_port(context.get_current_parameters().get("hypervisor_type"))
