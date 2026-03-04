"""TrueNorth Range - Provisioner registry.

Maps backend names to provisioner classes and provides a factory function.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseProvisioner

from .mock import MockProvisioner
from .terraform import TerraformProvisioner
from .proxmox_api import ProxmoxAPIProvisioner

_REGISTRY: dict[str, type] = {
    "mock": MockProvisioner,
    "terraform": TerraformProvisioner,
    "proxmox_api": ProxmoxAPIProvisioner,
}


def get_provisioner(backend: str) -> "BaseProvisioner":
    """Return an instantiated provisioner for the given backend name.

    Raises:
        ValueError: If the backend is not registered.
    """
    cls = _REGISTRY.get(backend)
    if cls is None:
        raise ValueError(
            f"Unknown provisioner backend: {backend!r}. "
            f"Available: {sorted(_REGISTRY)}"
        )
    return cls()


__all__ = [
    "MockProvisioner",
    "TerraformProvisioner",
    "ProxmoxAPIProvisioner",
    "get_provisioner",
]