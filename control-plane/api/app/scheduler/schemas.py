"""Scheduler request and response shapes. The names are part of the published contract
(docs/interfaces/openapi.json), so they are kept as they were in ``routers/scheduling.py``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from .models import OvercapacityPolicy


def as_utc(v: datetime) -> datetime:
    """Times are stored and compared in UTC (ADR 0004 §8). A time without a zone is taken
    as UTC; one with a zone is converted. Converting matters on SQLite, which keeps the
    wall time and drops the offset: 09:00-04:00 would otherwise be stored as 09:00."""
    return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)


UtcDateTime = Annotated[datetime, AfterValidator(as_utc)]


class EventIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    start_time: UtcDateTime
    end_time: UtcDateTime
    # Ignored when template_id is set: the template's VM specs decide the size.
    vm_count: int = Field(0, ge=0)
    vcpu_total: int = Field(0, ge=0)
    ram_mb_total: int = Field(0, ge=0)
    disk_gb_total: int = Field(0, ge=0)
    template_id: str | None = Field(None, description="Size the booking from this template's VM specs")
    range_id: str | None = None
    instructor_id: str | None = Field(
        None, description="Who teaches it; defaults to the caller when they are an instructor"
    )
    course_id: str | None = Field(None, description="The class: this course's active Students attend")
    scenario_id: str | None = Field(
        None, description="Run this scenario: a pending exercise is created when the range is built"
    )
    exercise_id: str | None = Field(None, description="Use this existing exercise (and its range) instead")
    draft: bool = Field(False, description="Create as a draft: holds nothing and is not checked until scheduled")


class EventOut(BaseModel):
    id: str
    name: str
    description: str | None
    state: str
    tenant_id: str
    range_id: str | None
    template_id: str | None
    instructor_id: str | None = None
    created_by: str | None = None
    course_id: str | None = None
    scenario_id: str | None = None
    exercise_id: str | None = None
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
    start_time: UtcDateTime
    end_time: UtcDateTime
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
    vm_count_needed: int = 0
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
    can_change: bool = Field(False, description="Whether the caller may change it (platform administrators only)")


class FeedTokenStatus(BaseModel):
    active: bool
    issued_at: datetime | None = None


class FeedTokenIssued(BaseModel):
    """Shown once: only a hash of the token is kept."""

    url: str = Field(description="HTTPS subscription URL; paste into Outlook 'Subscribe from web'")
    webcal_url: str = Field(description="The same URL as webcal://, for one-click subscribe")
    issued_at: datetime


class EventListOut(BaseModel):
    items: list[EventOut]
    total: int


class TimelineBucket(BaseModel):
    time: datetime
    vcpu_committed: int
    ram_mb_committed: int
    disk_gb_committed: int
    event_count: int


class TimelineOut(BaseModel):
    buckets: list[TimelineBucket]
    cluster_vcpu: int
    cluster_ram_mb: int
    cluster_disk_gb: int
    supply_source: str
    resolution_minutes: int
    lead_minutes: int
    grace_minutes: int


class MySessionOut(BaseModel):
    """A session as a Student sees it: no capacity, no other people."""

    id: str
    name: str
    description: str | None
    state: str
    start_time: datetime
    end_time: datetime
