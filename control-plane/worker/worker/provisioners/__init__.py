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


def discard_built(provisioner, range_id: str, result) -> dict:
    """Destroy what a provision built for a range that is no longer waiting for it (torn
    down mid-build, or its operation abandoned). Recording it would put live VMs under a
    destroyed range.

    Everything the build made goes to the teardown: VMs, port groups, mirror sessions
    (RSPAN) and the uplink. When the teardown is not ``ok`` (or raises), the result is
    ``discard_failed`` with the build's output as ``leftover``: worker/fencing.py records
    it on the range, so the API sees the VMs (``recorded_vms``) and refuses a new build
    over same-named leftovers until the range is destroyed."""
    import asyncio
    import logging

    output: dict = {"vms": result.vms, "networks": result.networks}
    for key in ("uplink", "mirrors"):
        if getattr(result, key, None):
            output[key] = getattr(result, key)
    try:
        outcome = asyncio.run(provisioner.destroy(range_id, output))
        status, errors = outcome.status, list(outcome.errors or [])
    except Exception as exc:  # noqa: BLE001 — reported below, with what is left
        status, errors = "failed", [str(exc) or type(exc).__name__]
    if status == "ok":
        return {"status": "discarded", "range_id": range_id, "vm_count": len(result.vms)}
    logging.getLogger("truenorth.worker").error(
        "range %s: discarding the %d VMs a build made FAILED (%s: %s); they are still on the hypervisor: %s",
        range_id, len(result.vms), status, "; ".join(errors), output,
    )
    return {"status": "discard_failed", "range_id": range_id, "vm_count": len(result.vms), "errors": errors,
            "leftover": output}


__all__ = [
    "discard_built",
    "MockProvisioner",
    "TerraformProvisioner",
    "ProxmoxAPIProvisioner",
    "VsphereAPIProvisioner",
    "HypervProvisioner",
    "get_provisioner",
]
