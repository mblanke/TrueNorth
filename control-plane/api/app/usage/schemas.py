# Author: Marc-Andre Blanke
# Created: 2026-11-28T02:58:11Z
"""Request and response shapes for usage and cost."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field


def _utc(v: datetime) -> datetime:
    return v.astimezone(UTC)


# A rate's start must carry its zone: a price guessed into the wrong zone starts hours off.
ZonedDateTime = Annotated[AwareDatetime, AfterValidator(_utc)]
Rate = Annotated[Decimal, Field(ge=0, le=Decimal("100000"), max_digits=14, decimal_places=6)]


class RateCardIn(BaseModel):
    effective_from: ZonedDateTime = Field(..., description="With a time zone offset; the card prices time from here on")
    currency: str = Field("CAD", pattern=r"^[A-Z]{3}$")
    per_vcpu_hour: Rate = Decimal(0)
    per_gb_ram_hour: Rate = Decimal(0)
    per_gb_disk_hour: Rate = Field(Decimal(0), description="Per GB-hour; the page shows it per GB-month (x730)")
    per_vm_hour: Rate = Field(Decimal(0), description="Per running VM-hour, e.g. guest OS licences")
    notes: str = Field("", max_length=1000)
    platform_wide: bool = Field(False, description="The default for every tenant (platform administrator only)")


class RateCardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID | None
    effective_from: datetime
    currency: str
    per_vcpu_hour: Decimal
    per_gb_ram_hour: Decimal
    per_gb_disk_hour: Decimal
    per_vm_hour: Decimal
    notes: str
    created_at: datetime | None = None


class Figures(BaseModel):
    vcpu_hours: float
    ram_gb_hours: float
    disk_gb_hours: float
    vm_hours: float
    cost_vcpu: float
    cost_ram: float
    cost_disk: float
    cost_vm: float
    cost_total: float
    currency: str = Field(..., description="Empty: nothing priced; 'mixed': cards in more than one currency")
    unpriced_hours: float = Field(..., description="Hours with no rate card in force: counted, not costed")


class CostRow(Figures):
    key: str = Field(..., description="The Student, course, class or tenant id; or e.g. 'unattributed:<tenant>'")
    label: str
    tenant_id: uuid.UUID | None
    intervals: int
    open: int = Field(..., description="Intervals still holding resources")
    approximate: bool = Field(..., description="Charged for the whole time held: no power history yet")


class CostReportOut(BaseModel):
    period_from: datetime
    period_to: datetime
    group_by: str
    rows: list[CostRow]
    totals: Figures


class UsageIntervalOut(Figures):
    source: str = Field(..., description="lab, class_seat, class_shared or unattributed")
    source_id: uuid.UUID
    range_id: uuid.UUID | None
    course_id: uuid.UUID | None
    class_id: uuid.UUID | None
    start: datetime
    end: datetime
    vcpu: float
    ram_gb: float
    disk_gb: float
    vms: float
    approximate: bool
    open: bool
    compute: bool = Field(..., description="False: the VMs were off (stopped), disk only")
    range_name: str | None = Field(None, description="None once the range row is gone")
    course_name: str | None = None
    class_name: str | None = None


class StudentCostOut(BaseModel):
    user_id: uuid.UUID
    period_from: datetime
    period_to: datetime
    intervals: list[UsageIntervalOut]
    totals: Figures
