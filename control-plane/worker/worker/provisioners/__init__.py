"""TrueNorth Range - Provisioner registry.

Maps backend names to provisioner classes and provides a factory function.

Supported: ``vsphere_api`` (ADR 0009) and ``mock``. ``proxmox_api`` and ``hyperv`` are
experimental: registered, but refused unless ``EXPERIMENTAL_PROVISIONERS`` is true
(the API refuses to create a range on them too, app/provisioner_choice.py). The
Terraform backends were removed: the worker image has no terraform binary and ADR 0009
rules ``terraform_vsphere`` out as a range builder.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseProvisioner

from .base import ExperimentalProvisionerError
from .hyperv import HypervProvisioner
from .mock import MockProvisioner
from .proxmox_api import ProxmoxAPIProvisioner
from .vsphere_api import VsphereAPIProvisioner

_REGISTRY: dict[str, type] = {
    "mock": MockProvisioner,
    # Direct API provisioners — one-per-hypervisor
    "proxmox_api": ProxmoxAPIProvisioner,
    "vsphere_api": VsphereAPIProvisioner,
    "hyperv": HypervProvisioner,
}
# Registered but off by default: no live run, no maintained deployment (vSphere only).
EXPERIMENTAL: frozenset[str] = frozenset({"proxmox_api", "hyperv"})
# Backends that take a HypervisorConnection's credentials (worker.db_ops.hypervisor_creds:
# the range's own tenant's endpoint and login), by the connection's hypervisor_type.
CREDENTIALS_HYPERVISOR: dict[str, str] = {"vsphere_api": "vsphere"}


def experimental_enabled() -> bool:
    """Whether EXPERIMENTAL_PROVISIONERS is set true (read on every call, not at import)."""
    return os.getenv("EXPERIMENTAL_PROVISIONERS", "").strip().lower() in {"1", "true", "yes", "on"}


def get_provisioner(backend: str, credentials: dict | None = None) -> BaseProvisioner:
    """Return an instantiated provisioner for the given backend name.

    ``credentials`` (a HypervisorConnection's endpoint and login) go to a backend that
    takes them (CREDENTIALS_HYPERVISOR); empty or None: the backend's own environment.

    Raises:
        ValueError: If the backend is not registered.
        ExperimentalProvisionerError: If it is experimental and EXPERIMENTAL_PROVISIONERS is off.
    """
    factory = _REGISTRY.get(backend)
    if factory is None:
        raise ValueError(f"Unknown provisioner backend: {backend!r}. Available: {sorted(_REGISTRY)}")
    if backend in EXPERIMENTAL and not experimental_enabled():
        raise ExperimentalProvisionerError(
            f"Provisioner backend {backend!r} is experimental and disabled. Supported: mock, vsphere_api. "
            "Set EXPERIMENTAL_PROVISIONERS=true on the API and the workers to use it anyway."
        )
    if credentials and backend in CREDENTIALS_HYPERVISOR:
        return factory(credentials=credentials)
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
    "EXPERIMENTAL",
    "ExperimentalProvisionerError",
    "MockProvisioner",
    "ProxmoxAPIProvisioner",
    "VsphereAPIProvisioner",
    "HypervProvisioner",
    "experimental_enabled",
    "get_provisioner",
]
