"""Network reservations: addresses and VLANs a range holds on shared infrastructure.

Noise agents' management NICs, edge-firewall uplinks and range VLANs share networks that
span tenants. They used to be derived (from template order) or allocated under a Redis
lock that was skipped when Redis was down, with no record the database could enforce.
Two ranges could get the same address. A reservation row is that record:

* ``domain`` names the shared network the value must be unique in, across all tenants
  (for example ``vsphere:vcsa01:TN-Noise-Mgmt``). Uniqueness is (domain, kind, value),
  not per tenant: per-tenant uniqueness would still let two tenants collide on one
  portgroup.
* ``holder`` is what in the range holds it (a node, a NIC). One reservation per (range,
  kind, holder) makes reserving idempotent.
* The row lives as long as the range holds the value: it is released when the range is
  destroyed, and deleted with the range.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin

KINDS = ("noise_mgmt_ip", "uplink_ip", "vlan")


class NetworkReservation(TimestampMixin, Base):
    __tablename__ = "network_reservations"
    __table_args__ = (
        UniqueConstraint("domain", "kind", "value", name="uq_network_reservations_value"),
        UniqueConstraint("range_id", "kind", "holder", name="uq_network_reservations_holder"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    range_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[str] = mapped_column(String(64), nullable=False)
    holder: Mapped[str] = mapped_column(String(255), nullable=False)
