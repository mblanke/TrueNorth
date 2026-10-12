"""The port a hypervisor connection gets when none is given, by family.

Until 2026-10-11 every connection defaulted to 8006 (Proxmox's API port), so a vSphere
connection created without a port was stored as 8006. vCenter answers on 443, Hyper-V's
WinRM on 5985. No imports from the app: schemas and models may use this.
"""

from __future__ import annotations

DEFAULT_PORTS: dict[str, int] = {"vsphere": 443, "proxmox": 8006, "hyperv": 5985}


def default_port(hypervisor_type: str | None) -> int:
    return DEFAULT_PORTS.get(hypervisor_type or "", 8006)


def endpoint_key(url: str) -> tuple[str, int]:
    """(host, port) of an endpoint URL (hypervisor_backends.vsphere.base_url, or a URL the
    worker recorded), for comparing two spellings of one vCenter."""
    from urllib.parse import urlparse

    url = str(url or "")
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        return (parsed.hostname or "").lower(), parsed.port or 443
    except ValueError:
        return url, 0


def column_default(context) -> int:
    """SQLAlchemy column default (models.HypervisorConnection.port): the row's family's port."""
    return default_port(context.get_current_parameters().get("hypervisor_type"))
