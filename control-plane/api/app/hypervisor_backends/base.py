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

    @abstractmethod
    def check_connection(self, conn: HypervisorConnection, db: Session) -> HypervisorTestResult:
        """Reach the hypervisor with the stored credentials; update ``conn.is_active``."""

    @abstractmethod
    def discover(self, conn_id: uuid.UUID | str, conn: HypervisorConnection, db: Session) -> dict:
        """Upsert the connection's hosts into ``hypervisor_nodes``.

        Returns ``{"message": str, "nodes_discovered": int}``. Never raises for a
        hypervisor-side failure; report it in ``message``.
        """
