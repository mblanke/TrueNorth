"""TrueNorth Range - Abstract base provisioner.

All provisioner backends must implement this interface.
"""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

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


class ExperimentalProvisionerError(ValueError):
    """An experimental backend (proxmox_api, hyperv) was selected without
    EXPERIMENTAL_PROVISIONERS. Final: a retry finds the same configuration."""


@dataclass(frozen=True)
class AllocationNeed:
    """Values on shared infrastructure a build needs reserved first (``allocation_needs``).

    The worker reserves ``holders`` out of ``pool`` on the ``network_reservations`` table
    (unique per domain and kind across all tenants; worker/range_alloc.py) and hands the
    result to ``provision`` as ``allocations[key]``: ``{holder: value}``, or the one value
    when ``single``.
    """

    key: str  # where provision() finds the result in ``allocations``
    kind: str  # a network_reservations kind: "vlan", "uplink_ip", ...
    domain: str  # the shared network the values must be unique in
    pool: list[str]  # candidate values, in the order they are handed out
    holders: list[str]  # what in the range holds a value (a logical VLAN, "edge")
    single: bool = False


@dataclass(frozen=True)
class GuestLogin:
    """An account inside a range VM (``family``: linux | windows). The password is never
    logged; providers redact it from any error they record."""

    username: str
    password: str = field(repr=False)
    family: str = "linux"


@dataclass(frozen=True)
class GuestStep:
    """One program to run in a guest: ``program`` + ``arguments`` (one string, as the
    guest's process API takes it); ``ok_codes`` are the exit codes that count as ok."""

    label: str
    program: str
    arguments: str = ""
    ok_codes: tuple[int, ...] = (0,)


class BaseProvisioner(ABC):
    """Abstract base class for range provisioning backends."""

    def allocation_needs(self, range_id: str, template: dict) -> list[AllocationNeed]:
        """Shared values (VLANs, addresses) to reserve before ``provision``. Default: none."""
        return []

    def planned_output(self, range_id: str, allocations: dict) -> dict:
        """What to record in provisioner_output before the build, so a destroy after a
        build that died half-way still finds what was reserved. Default: nothing."""
        return {}

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

    async def find_vms(self, name_prefix: str) -> list[dict]:
        """VMs on the hypervisor whose name starts with ``name_prefix``, as
        ``[{"vm_id", "name"}]``. Reconciliation uses it to find what a range left behind
        after its records were lost; a backend that cannot list VMs returns []."""
        return []

    async def collect_metrics(
        self,
        range_id: str,
        provision_output: dict,
    ) -> MetricsResult:
        """Resource usage of every VM in a range (see MetricsResult for the fields).

        Concrete, like restore: a backend that cannot read metrics says so, rather than
        anyone filling the gap with made-up numbers.
        """
        return MetricsResult(
            status="unsupported",
            errors=[f"{type(self).__name__} does not report VM metrics"],
        )

    supports_guest_commands: bool = False

    async def run_in_guest(
        self,
        vm_id: str,
        login: GuestLogin,
        steps: list[GuestStep],
        timeout: float,
    ) -> list[dict]:
        """Run ``steps`` in order inside one range VM's guest OS, as ``login``, with no
        network path into the range (vSphere: VMware guest operations). Stops at the
        first step that does not end ``ok``.

        Returns one ``{"label", "status": ok | failed | timeout | not_run, "exit_code"}``
        per step. The post-deploy configure stage (worker/greyspace.py) and Greyspace
        breadcrumbs use it. A backend that cannot reach into a guest says so here, so
        callers record why instead of guessing (``supports_guest_commands``).
        """
        raise NotImplementedError(f"{type(self).__name__} cannot run commands inside a guest")

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[BaseProvisioner]:
        """Scope for several calls in a row (one scheduled run over many ranges).

        A backend that logs in to its hypervisor may keep one login for the whole scope
        and log out at its end, instead of one login per range per run. Default: nothing.
        """
        yield self
