"""Hypervisor connection backend interface (docs/adr/0001-adapter-registry.md).

The API side of hypervisor support: checking a stored connection and discovering its
hosts into ``hypervisor_nodes``. VM lifecycle lives in the worker's provisioners
(``control-plane/worker/worker/provisioners/``); this interface is only what the
Infrastructure UI needs from a connection.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from sqlalchemy.orm import Session

from ..models import HypervisorConnection
from ..schemas import HypervisorTestResult


class BaseHypervisorBackend(ABC):
    """One hypervisor family (vSphere, Proxmox, Hyper-V, ...)."""

    #: ``hypervisor_connections.hypervisor_type`` value this backend serves.
    kind: str

    #: Whether a connection must carry a password. The worker logs in with the
    #: connection's own credentials only, never the platform's (worker
    #: provisioners/vsphere_api.py use_credentials), so one without is unusable.
    requires_password: bool = False

    #: The worker provisioner backends (``ranges.provisioner_backend``) that build ranges
    #: with this family's connections and record which one (worker/connection_binding.py).
    #: A connection with live ranges of these on it cannot be deactivated, repointed or
    #: deleted (app/hypervisor_connection_guard.py). Must match the worker's
    #: CREDENTIALS_HYPERVISOR (tests/contracts).
    provisioner_backends: frozenset[str] = frozenset()

    @abstractmethod
    def check_connection(self, conn: HypervisorConnection, db: Session) -> HypervisorTestResult:
        """Reach the hypervisor with the stored credentials; update ``conn.is_active``."""

    @abstractmethod
    def discover(self, conn_id: uuid.UUID | str, conn: HypervisorConnection, db: Session) -> dict:
        """Upsert the connection's hosts into ``hypervisor_nodes``.

        Returns ``{"message": str, "nodes_discovered": int}``, plus ``"error": str`` when
        discovery failed. Never raises for a hypervisor-side failure; report it in
        ``message`` and ``error``. The scheduled rediscovery (app/infra_discovery) records
        ``error`` as the connection's last error and keeps its last success time.
        """

    def list_vms(self, conn: HypervisorConnection) -> tuple[list[dict], str] | None:
        """The connection's VMs, read live: ``([{vm_id, name, power_state, host, tags}], note)``
        with ``tags`` TrueNorth's tn-* tags (category -> value). Raises on a hypervisor-side
        failure. None: this family cannot list them (the page says so; never an empty list)."""
        return None
