"""TrueNorth Range - Provisioner result dataclasses.

Typed result objects returned by all provisioner operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProvisionResult:
    """Result of a range provisioning operation."""

    status: str  # ok, partial, failed
    vms: list[dict] = field(default_factory=list)
    networks: list[dict] = field(default_factory=list)
    duration_seconds: float = 0.0
    terraform_state: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class DestroyResult:
    """Result of a range destroy operation."""

    status: str  # ok, partial, failed
    duration_seconds: float = 0.0
    resources_removed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class StopResult:
    """Result of stopping a range."""

    status: str  # ok, partial, failed
    vms_stopped: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class StartResult:
    """Result of starting a range."""

    status: str  # ok, partial, failed
    vms_started: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class SnapshotResult:
    """Result of a snapshot operation."""

    status: str  # ok, partial, failed
    snapshot_name: str = ""
    vms_snapped: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class HealthResult:
    """Result of a health-check operation."""

    healthy: bool = True
    status: str = "ok"  # ok, degraded, unhealthy
    vm_statuses: list[dict] = field(default_factory=list)
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
