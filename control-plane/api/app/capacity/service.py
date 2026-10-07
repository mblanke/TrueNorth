"""Supply and running load (docs/adr/0006-capacity-service.md).

Supply
    Each online host of an active hypervisor connection contributes what discovery
    recorded for it. vCPU is physical cores times ``CAPACITY_VCPU_RATIO`` (default 4);
    RAM and disk are 1:1 unless ``CAPACITY_RAM_RATIO`` / ``CAPACITY_DISK_RATIO`` say
    otherwise. ``CLUSTER_OVERHEAD_PCT`` (default 15) is held back for the hypervisor.

    A resource no host reports falls back to its ``CLUSTER_TOTAL_*`` env value, and
    ``source`` says which resources came from where. vCenter's REST API reports no
    host CPU or memory, so on vSphere the figures come from discovery that reads the
    hardware (PR #3, S5a); until it runs, vSphere supply is the env fallback, as before.

Running load
    A range that is built (provisioning, ready, running) holds its template's size
    until it is destroyed; a stopped range holds its disk only. The scheduler counts a
    range through its booking while one covers the window, and through this otherwise,
    which closes the gap a range started without a booking used to leave.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import HypervisorConnection, HypervisorNode, Range, RangeState, Template
from ..range_topology import template_demand

GB = 1024  # MB


@dataclass(frozen=True)
class ClusterSupply:
    vcpu: int
    ram_mb: int
    disk_gb: int
    source: str  # "discovered" | "env" | "discovered (disk: env)" ...
    hosts: int


@dataclass(frozen=True)
class RangeLoad:
    range_id: uuid.UUID
    vcpu: int
    ram_mb: int
    disk_gb: int


def _env_int(name: str, default: str) -> int:
    return int(os.getenv(name, default))


def _env_float(name: str, default: str) -> float:
    return float(os.getenv(name, default))


def supply(db: Session) -> ClusterSupply:
    """Usable cluster capacity after overcommit and headroom."""
    hosts = (
        db.query(HypervisorNode)
        .join(HypervisorConnection, HypervisorConnection.id == HypervisorNode.connection_id)
        .filter(HypervisorConnection.is_active == True, HypervisorNode.status == "online")  # noqa: E712
        .all()
    )
    keep = 1 - _env_float("CLUSTER_OVERHEAD_PCT", "15") / 100

    def total(values: list[float | None]) -> float | None:
        known = [v for v in values if v is not None and v > 0]
        # Every online host must report it: a partial sum would understate the cluster
        # and look like real data.
        return sum(known) if hosts and len(known) == len(hosts) else None

    cores = total([h.cpu_total for h in hosts])
    ram_gb = total([h.memory_total_gb for h in hosts])
    disk_gb = total([h.storage_total_gb for h in hosts])

    from_env: list[str] = []
    if cores is None:
        from_env.append("vCPU")
        vcpu = _env_int("CLUSTER_TOTAL_VCPU", "128")
    else:
        vcpu = cores * _env_float("CAPACITY_VCPU_RATIO", "4")
    if ram_gb is None:
        from_env.append("RAM")
        ram_mb = _env_int("CLUSTER_TOTAL_RAM_MB", "524288")
    else:
        ram_mb = ram_gb * GB * _env_float("CAPACITY_RAM_RATIO", "1")
    if disk_gb is None:
        from_env.append("disk")
        disk = _env_int("CLUSTER_TOTAL_DISK_GB", "10240")
    else:
        disk = disk_gb * _env_float("CAPACITY_DISK_RATIO", "1")

    if len(from_env) == 3:
        source = "env"
    elif from_env:
        source = f"discovered ({', '.join(from_env)}: env)"
    else:
        source = "discovered"
    return ClusterSupply(int(vcpu * keep), int(ram_mb * keep), int(disk * keep), source, len(hosts))


BUILT = (RangeState.provisioning, RangeState.ready, RangeState.running)


def running_ranges(db: Session) -> list[RangeLoad]:
    """Every range holding cluster resources now, sized from its template."""
    rows = (
        db.query(Range.id, Range.state, Template.yaml)
        .join(Template, Template.id == Range.template_id)
        .filter(Range.state.in_((*BUILT, RangeState.stopped)), Range.deleted_at.is_(None))
        .all()
    )
    out: list[RangeLoad] = []
    for rid, state, yaml in rows:
        d = template_demand(yaml)
        if d is None:
            continue
        if state == RangeState.stopped:  # powered off: its disks remain
            out.append(RangeLoad(rid, 0, 0, d.disk_gb))
        else:
            out.append(RangeLoad(rid, d.vcpu, d.ram_mb, d.disk_gb))
    return out
