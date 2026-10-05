"""Scheduler request and response shapes. The names are part of the published contract
(docs/interfaces/openapi.json), so they are kept as they were in ``routers/scheduling.py``."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .models import OvercapacityPolicy


class EventIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    start_time: datetime
    end_time: datetime
    # Ignored when template_id is set: the template's VM specs decide the size.
    vm_count: int = Field(0, ge=0)
    vcpu_total: int = Field(0, ge=0)
    ram_mb_total: int = Field(0, ge=0)
    disk_gb_total: int = Field(0, ge=0)
    template_id: str | None = Field(None, description="Size the booking from this template's VM specs")
    range_id: str | None = None


class EventOut(BaseModel):
    id: str
    name: str
    description: str | None
    state: str
    tenant_id: str
    range_id: str | None
    template_id: str | None
    start_time: datetime
    end_time: datetime
    vm_count: int
    vcpu_total: int
    ram_mb_total: int
    disk_gb_total: int
    created_at: datetime
    updated_at: datetime
    # Over-capacity warnings, when the policy is `warn` and the booking did not fit.
    warnings: list[str] = []

    class Config:
        from_attributes = True


class CapacityCheck(BaseModel):
    start_time: datetime
    end_time: datetime
    vcpu_needed: int = 0
    ram_mb_needed: int = 0
    disk_gb_needed: int = 0
    template_id: str | None = Field(
        None, description="Size the check from this template; overrides the *_needed values"
    )


class CapacityResult(BaseModel):
    fits: bool
    vcpu_available: int
    vcpu_committed: int
    vcpu_total: int
    ram_mb_available: int
    ram_mb_committed: int
    ram_mb_total: int
    disk_gb_available: int
    disk_gb_committed: int
    disk_gb_total: int
    overlapping_events: int
    message: str
    # What was asked for (from the template when one was given) and why it does not fit.
    vcpu_needed: int = 0
    ram_mb_needed: int = 0
    disk_gb_needed: int = 0
    reasons: list[str] = []
    policy: str = "block"
    supply_source: str = Field("env", description="Where cluster totals came from: env fallback, or discovery")


class PolicyIn(BaseModel):
    overcapacity: OvercapacityPolicy


class PolicyOut(BaseModel):
    overcapacity: OvercapacityPolicy
