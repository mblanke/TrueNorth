# Author: Marc-Andre Blanke
# Created: 2026-11-28T02:58:11Z
"""Pricing an interval with the rate card in force for each part of it.

The card in force for a tenant at time t is that tenant's latest card with
``effective_from <= t``; with none, the platform default's (``tenant_id`` null). An
interval that crosses a card change is split there and each part priced with its own card.
A part with no card in force is unpriced: its hours count, its cost is zero, and the row
says so.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from .models import RateCard

HOURS_PER_MONTH = 730  # how the page shows the disk rate per GB-month


@dataclass(frozen=True)
class Card:
    effective_from: datetime
    currency: str
    vcpu: float
    ram_gb: float
    disk_gb: float
    vm: float


@dataclass
class Priced:
    vcpu_hours: float = 0.0
    ram_gb_hours: float = 0.0
    disk_gb_hours: float = 0.0
    vm_hours: float = 0.0
    cost_vcpu: float = 0.0
    cost_ram: float = 0.0
    cost_disk: float = 0.0
    cost_vm: float = 0.0
    unpriced_hours: float = 0.0
    currency: str = ""

    @property
    def total(self) -> float:
        return self.cost_vcpu + self.cost_ram + self.cost_disk + self.cost_vm

    def add(self, other: Priced) -> None:
        for name in (
            "vcpu_hours",
            "ram_gb_hours",
            "disk_gb_hours",
            "vm_hours",
            "cost_vcpu",
            "cost_ram",
            "cost_disk",
            "cost_vm",
            "unpriced_hours",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        if other.currency and not self.currency:
            self.currency = other.currency
        elif other.currency and other.currency != self.currency:
            self.currency = "mixed"


def _card(row: RateCard) -> Card:
    return Card(
        effective_from=_aware(row.effective_from),
        currency=row.currency,
        vcpu=float(row.per_vcpu_hour),
        ram_gb=float(row.per_gb_ram_hour),
        disk_gb=float(row.per_gb_disk_hour),
        vm=float(row.per_vm_hour),
    )


def _aware(t: datetime) -> datetime:
    from datetime import UTC

    return t if t.tzinfo else t.replace(tzinfo=UTC)


class RateBook:
    """Every card that can price a set of tenants, loaded once per report."""

    def __init__(self, db: Session, tenant_ids: set[uuid.UUID] | None):
        q = db.query(RateCard)
        if tenant_ids is not None:
            q = q.filter((RateCard.tenant_id.is_(None)) | (RateCard.tenant_id.in_(list(tenant_ids))))
        self._by_tenant: dict[uuid.UUID | None, list[Card]] = {}
        for row in q.order_by(RateCard.effective_from, RateCard.created_at).all():
            self._by_tenant.setdefault(row.tenant_id, []).append(_card(row))

    @classmethod
    def of(cls, cards: dict[uuid.UUID | None, list[Card]]) -> RateBook:
        book = cls.__new__(cls)
        book._by_tenant = {k: sorted(v, key=lambda c: c.effective_from) for k, v in cards.items()}
        return book

    def in_force(self, tenant_id: uuid.UUID | None, at: datetime) -> Card | None:
        for key in (tenant_id, None) if tenant_id is not None else (None,):
            found = None
            for card in self._by_tenant.get(key, []):
                if card.effective_from <= at:
                    found = card
                else:
                    break
            if found is not None:
                return found
        return None

    def _changes(self, tenant_id: uuid.UUID | None, start: datetime, end: datetime) -> list[datetime]:
        keys = (tenant_id, None) if tenant_id is not None else (None,)
        return sorted(
            {c.effective_from for k in keys for c in self._by_tenant.get(k, []) if start < c.effective_from < end}
        )

    def price(
        self,
        tenant_id: uuid.UUID | None,
        start: datetime,
        end: datetime,
        *,
        vcpu: float,
        ram_gb: float,
        disk_gb: float,
        vms: float,
        compute: bool = True,
    ) -> Priced:
        """Price ``[start, end)`` of an allocation. ``compute`` False: disk only (a stopped range)."""
        out = Priced()
        if end <= start:
            return out
        points = [start, *self._changes(tenant_id, start, end), end]
        for a, b in zip(points, points[1:], strict=False):
            hours = (b - a).total_seconds() / 3600.0
            part = Priced(
                vcpu_hours=vcpu * hours if compute else 0.0,
                ram_gb_hours=ram_gb * hours if compute else 0.0,
                disk_gb_hours=disk_gb * hours,
                vm_hours=vms * hours if compute else 0.0,
            )
            card = self.in_force(tenant_id, a)
            if card is None:
                part.unpriced_hours = hours
            else:
                part.currency = card.currency
                part.cost_vcpu = part.vcpu_hours * card.vcpu
                part.cost_ram = part.ram_gb_hours * card.ram_gb
                part.cost_disk = part.disk_gb_hours * card.disk_gb
                part.cost_vm = part.vm_hours * card.vm
            out.add(part)
        return out
