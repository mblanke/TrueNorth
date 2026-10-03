"""TrueNorth Range - Provisioner result dataclasses.

Typed result objects returned by all provisioner operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def outcome(succeeded: int, errors: list[str]) -> str:
    """``ok`` when nothing failed, ``failed`` when nothing succeeded, else ``partial``."""
    if not errors:
        return "ok"
    return "partial" if succeeded else "failed"


@dataclass
class ProvisionResult:
    """Result of a range provisioning operation."""

    status: str  # ok, partial, failed
    vms: list[dict] = field(default_factory=list)
    networks: list[dict] = field(default_factory=list)
    duration_seconds: float = 0.0
    terraform_state: str | None = None
    errors: list[str] = field(default_factory=list)
    # Things skipped on purpose that do not make the build partial (vSphere: software
    # names not in the catalogue, installs skipped for want of a depot path).
    warnings: list[str] = field(default_factory=list)
    # vSphere: the edge firewall's WAN address on the depot uplink network, if any.
    uplink: dict | None = None


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
class RestoreResult:
    """Result of reverting a range to a snapshot."""

    status: str  # ok, partial, failed
    snapshot_name: str = ""
    vms_restored: int = 0  # reverted and, where asked, powered on
    # Reverted at all, powered on or not. Zero means the range was not touched, so
    # a failure leaves it as it was rather than half-reverted.
    vms_reverted: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class SnapshotDeleteResult:
    """Result of deleting a snapshot from every VM in a range."""

    status: str  # ok, partial, failed
    snapshot_name: str = ""
    vms_cleaned: int = 0
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
