"""TrueNorth Range - Provisioner registry.

Maps backend names to provisioner classes and provides a factory function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseProvisioner

from .hyperv import HypervProvisioner
from .mock import MockProvisioner
from .proxmox_api import ProxmoxAPIProvisioner
from .terraform import TerraformProvisioner
from .vsphere_api import VsphereAPIProvisioner

_REGISTRY: dict[str, type] = {
    "mock": MockProvisioner,
    # Direct API provisioners — one-per-hypervisor
    "proxmox_api": ProxmoxAPIProvisioner,
    "vsphere_api": VsphereAPIProvisioner,
    "hyperv": HypervProvisioner,
    # Terraform provisioners — one entry per supported hypervisor type.
    # These share the same TerraformProvisioner class but use different
    # template directories (TERRAFORM_<TYPE>_DIR env vars).
    "terraform": TerraformProvisioner,  # default — proxmox
    "terraform_proxmox": lambda: TerraformProvisioner(hypervisor_type="proxmox"),
    "terraform_vsphere": lambda: TerraformProvisioner(hypervisor_type="vsphere"),
    "terraform_hyperv": lambda: TerraformProvisioner(hypervisor_type="hyperv"),
}


def get_provisioner(backend: str) -> BaseProvisioner:
    """Return an instantiated provisioner for the given backend name.

    Raises:
        ValueError: If the backend is not registered.
    """
    factory = _REGISTRY.get(backend)
    if factory is None:
        raise ValueError(f"Unknown provisioner backend: {backend!r}. Available: {sorted(_REGISTRY)}")
    return factory()


__all__ = [
    "MockProvisioner",
    "TerraformProvisioner",
    "ProxmoxAPIProvisioner",
    "VsphereAPIProvisioner",
    "HypervProvisioner",
    "get_provisioner",
]
