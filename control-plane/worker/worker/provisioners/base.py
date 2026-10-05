"""TrueNorth Range - Abstract base provisioner.

All provisioner backends must implement this interface.
"""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from .results import (
    DestroyResult,
    HealthResult,
    MetricsResult,
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

    async def collect_metrics(
        self,
        range_id: str,
        provision_output: dict,
    ) -> MetricsResult:
        """Resource usage of every VM in a range (see MetricsResult for the fields).

        Concrete, like restore: a backend that cannot read metrics says so. The worker
        used to fill this gap with random numbers.
        """
        return MetricsResult(
            status="unsupported",
            errors=[f"{type(self).__name__} does not report VM metrics"],
        )

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[BaseProvisioner]:
        """Scope for several calls in a row (one scheduled run over many ranges).

        A backend that logs in to its hypervisor may keep one login for the whole scope
        and log out at its end, instead of one login per range per run. Default: nothing.
        """
        yield self
