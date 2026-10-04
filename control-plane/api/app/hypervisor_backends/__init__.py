"""TrueNorth Range — hypervisor connection backend registry.

Usage:
    from app.hypervisor_backends import get_hypervisor_backend

    backend = get_hypervisor_backend(conn.hypervisor_type)  # None if unsupported
    result = backend.check_connection(conn, db)

Adding a hypervisor family:
    1. Create hypervisor_backends/<name>.py implementing BaseHypervisorBackend
    2. Add it to _REGISTRY below
    3. tests/contracts covers it automatically; routers need no change.

Vendor SDKs (proxmoxer, pyVmomi, winrm) are imported only inside this package.
"""

from __future__ import annotations

from .base import BaseHypervisorBackend
from .hyperv import HyperVBackend
from .proxmox import ProxmoxBackend
from .vsphere import VSphereBackend

__all__ = [
    "BaseHypervisorBackend",
    "HyperVBackend",
    "ProxmoxBackend",
    "VSphereBackend",
    "get_hypervisor_backend",
    "supported_hypervisor_types",
]

_REGISTRY: dict[str, type[BaseHypervisorBackend]] = {
    VSphereBackend.kind: VSphereBackend,
    ProxmoxBackend.kind: ProxmoxBackend,
    HyperVBackend.kind: HyperVBackend,
}


def get_hypervisor_backend(kind: str | None) -> BaseHypervisorBackend | None:
    """Return the backend for a ``hypervisor_type``, or None if none is registered."""
    cls = _REGISTRY.get(kind or "")
    return cls() if cls else None


def supported_hypervisor_types() -> list[str]:
    return sorted(_REGISTRY)
