"""TrueNorth Range - Mock provisioner for development and testing.

Simulates infrastructure operations with configurable delays and failure rates.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
from datetime import UTC, datetime

from .base import BaseProvisioner
from .results import (
    DestroyResult,
    HealthResult,
    ProvisionResult,
    SnapshotResult,
    StartResult,
    StopResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
MOCK_PROVISION_DELAY: float = float(os.environ.get("MOCK_PROVISION_DELAY", "0.1"))
MOCK_FAILURE_RATE: float = float(os.environ.get("MOCK_FAILURE_RATE", "0.0"))


def _should_fail() -> bool:
    """Return True with probability MOCK_FAILURE_RATE."""
    return random.random() < MOCK_FAILURE_RATE


def _gen_ip(index: int) -> str:
    """Generate a deterministic mock IP address."""
    return f"10.0.{(index // 254) + 1}.{(index % 254) + 1}"


class MockProvisioner(BaseProvisioner):
    """In-memory mock provisioner for tests and local development."""

    def __init__(self) -> None:
        self._state: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # provision
    # ------------------------------------------------------------------ #
    async def provision(
        self,
        range_id: str,
        template: dict,
        allocations: dict,
    ) -> ProvisionResult:
        start = time.monotonic()
        await asyncio.sleep(MOCK_PROVISION_DELAY)

        if _should_fail():
            logger.warning("MockProvisioner: simulated provision failure for %s", range_id)
            return ProvisionResult(
                status="failed",
                errors=["Simulated provision failure"],
                duration_seconds=time.monotonic() - start,
            )

        vm_defs = template.get("vms", [{"name": "default-vm"}])
        vms = []
        for idx, vm_def in enumerate(vm_defs):
            vm = {
                "vm_id": str(uuid.uuid4()),
                "name": vm_def.get("name", f"vm-{idx}"),
                "ip": _gen_ip(idx),
                "status": "running",
                "cpu": vm_def.get("cpu", 2),
                "memory_mb": vm_def.get("memory_mb", 4096),
            }
            vms.append(vm)

        networks = [
            {
                "network_id": str(uuid.uuid4()),
                "name": "mock-net-0",
                "cidr": "10.0.1.0/24",
                "vlan_id": 100,
            }
        ]

        self._state[range_id] = {
            "vms": vms,
            "networks": networks,
            "status": "running",
            "provisioned_at": datetime.now(UTC).isoformat(),
        }

        # Mock telemetry
        logger.info(
            "MockProvisioner: provisioned %d VMs for range %s in %.2fs",
            len(vms),
            range_id,
            time.monotonic() - start,
        )

        return ProvisionResult(
            status="ok",
            vms=vms,
            networks=networks,
            duration_seconds=time.monotonic() - start,
            terraform_state=None,
        )

    # ------------------------------------------------------------------ #
    # destroy
    # ------------------------------------------------------------------ #
    async def destroy(
        self,
        range_id: str,
        provision_output: dict,
    ) -> DestroyResult:
        start = time.monotonic()
        await asyncio.sleep(MOCK_PROVISION_DELAY * 0.5)

        if _should_fail():
            return DestroyResult(
                status="failed",
                errors=["Simulated destroy failure"],
                duration_seconds=time.monotonic() - start,
            )

        state = self._state.pop(range_id, {})
        removed = len(state.get("vms", [])) + len(state.get("networks", []))

        logger.info("MockProvisioner: destroyed range %s (%d resources)", range_id, removed)
        return DestroyResult(
            status="ok",
            resources_removed=removed,
            duration_seconds=time.monotonic() - start,
        )

    # ------------------------------------------------------------------ #
    # stop
    # ------------------------------------------------------------------ #
    async def stop(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StopResult:
        start = time.monotonic()
        await asyncio.sleep(MOCK_PROVISION_DELAY * 0.3)

        state = self._state.get(range_id, {})
        vms = state.get("vms", [])
        for vm in vms:
            vm["status"] = "stopped"
        if state:
            state["status"] = "stopped"

        return StopResult(
            status="ok",
            vms_stopped=len(vms),
            duration_seconds=time.monotonic() - start,
        )

    # ------------------------------------------------------------------ #
    # start
    # ------------------------------------------------------------------ #
    async def start(
        self,
        range_id: str,
        provision_output: dict,
    ) -> StartResult:
        start = time.monotonic()
        await asyncio.sleep(MOCK_PROVISION_DELAY * 0.3)

        state = self._state.get(range_id, {})
        vms = state.get("vms", [])
        for vm in vms:
            vm["status"] = "running"
        if state:
            state["status"] = "running"

        return StartResult(
            status="ok",
            vms_started=len(vms),
            duration_seconds=time.monotonic() - start,
        )

    # ------------------------------------------------------------------ #
    # snapshot
    # ------------------------------------------------------------------ #
    async def snapshot(
        self,
        range_id: str,
        provision_output: dict,
        name: str,
    ) -> SnapshotResult:
        start = time.monotonic()
        await asyncio.sleep(MOCK_PROVISION_DELAY * 0.5)

        if _should_fail():
            return SnapshotResult(
                status="failed",
                snapshot_name=name,
                errors=["Simulated snapshot failure"],
                duration_seconds=time.monotonic() - start,
            )

        state = self._state.get(range_id, {})
        vms = state.get("vms", [])

        logger.info("MockProvisioner: snapshot '%s' for range %s (%d VMs)", name, range_id, len(vms))
        return SnapshotResult(
            status="ok",
            snapshot_name=name,
            vms_snapped=len(vms),
            duration_seconds=time.monotonic() - start,
        )

    # ------------------------------------------------------------------ #
    # health_check
    # ------------------------------------------------------------------ #
    async def health_check(
        self,
        range_id: str,
        provision_output: dict,
    ) -> HealthResult:
        start = time.monotonic()
        await asyncio.sleep(MOCK_PROVISION_DELAY * 0.1)

        state = self._state.get(range_id)
        if state is None:
            return HealthResult(
                healthy=False,
                status="unhealthy",
                errors=[f"Range {range_id} not found in mock state"],
                duration_seconds=time.monotonic() - start,
            )

        vm_statuses = []
        all_healthy = True
        for vm in state.get("vms", []):
            healthy = vm.get("status") == "running"
            if not healthy:
                all_healthy = False
            vm_statuses.append(
                {
                    "vm_id": vm["vm_id"],
                    "name": vm["name"],
                    "status": vm.get("status", "unknown"),
                    "healthy": healthy,
                }
            )

        overall = "ok" if all_healthy else "degraded"
        return HealthResult(
            healthy=all_healthy,
            status=overall,
            vm_statuses=vm_statuses,
            duration_seconds=time.monotonic() - start,
        )
