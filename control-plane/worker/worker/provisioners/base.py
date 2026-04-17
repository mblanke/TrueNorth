"""TrueNorth Range - Abstract base provisioner.

All provisioner backends must implement this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .results import (
    DestroyResult,
    HealthResult,
    ProvisionResult,
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

    @abstractmethod
    async def health_check(
        self,
        range_id: str,
        provision_output: dict,
    ) -> HealthResult:
        """Check health of all infrastructure in a range."""
        ...
