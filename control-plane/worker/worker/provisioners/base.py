"""TrueNorth Range - Abstract base provisioner.

All provisioner backends must implement this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .results import (
    DestroyResult,
    HealthResult,
    ProvisionResult,
    RestoreResult,
    SnapshotDeleteResult,
    SnapshotResult,
    StartResult,
    StopResult,
)


class BaseProvisioner(ABC):
    """Abstract base class for range provisioning backends."""

    @abstractmethod
    async def provision(
        self,
        range_id: str,
        template: dict,
        allocations: dict,
    ) -> ProvisionResult:
        """Provision infrastructure for a range."""
        ...

    @abstractmethod
    async def destroy(
        self,
        range_id: str,
        provision_output: dict,
    ) -> DestroyResult:
        """Destroy all infrastructure for a range."""
        ...

    @abstractmethod
    async def stop(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StopResult:
        """Stop (power off) all VMs in a range."""
        ...

    @abstractmethod
    async def start(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StartResult:
        """Start (power on) all VMs in a range."""
        ...

    @abstractmethod
    async def snapshot(
        self,
        range_id: str,
        provision_output: dict,
        name: str,
    ) -> SnapshotResult:
        """Create a snapshot of all VMs in a range."""
        ...

    # restore/delete_snapshot are concrete, not abstract: a backend that cannot do them
    # must say so in a failed result the worker can act on. They used to be missing
    # altogether, so the worker's calls died with AttributeError on every backend.
    async def restore(
        self,
        range_id: str,
        provision_output: dict,
        name: str,
        power_on: bool,
    ) -> RestoreResult:
        """Revert every VM in a range to the snapshot called ``name``.

        ``power_on`` says what state the range was in when the snapshot was taken:
        True leaves the VMs running afterwards, False leaves them as the revert did.
        """
        return RestoreResult(
            status="failed",
            snapshot_name=name,
            errors=[f"{type(self).__name__} does not support snapshot restore"],
        )

    async def delete_snapshot(
        self,
        range_id: str,
        provision_output: dict,
        name: str,
    ) -> SnapshotDeleteResult:
        """Remove the snapshot called ``name`` from every VM in a range.

        A VM that no longer has the snapshot counts as cleaned, so a retry is safe.
        """
        return SnapshotDeleteResult(
            status="failed",
            snapshot_name=name,
            errors=[f"{type(self).__name__} does not support snapshot deletion"],
        )

    @abstractmethod
    async def health_check(
        self,
        range_id: str,
        provision_output: dict,
    ) -> HealthResult:
        """Check health of all infrastructure in a range."""
        ...
