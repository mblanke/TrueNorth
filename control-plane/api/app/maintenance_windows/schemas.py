"""Maintenance window request and response shapes (published in docs/interfaces/openapi.json)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, Field, field_validator, model_validator


def _utc(v: datetime) -> datetime:
    return v.astimezone(UTC)


# A window's times must carry their zone (an offset or Z): a maintenance window guessed
# into the wrong zone opens or closes hours away from what the admin meant. Converted to
# UTC, which is how they are stored and compared.
ZonedDateTime = Annotated[AwareDatetime, AfterValidator(_utc)]


class WindowScope(str, Enum):
    all = "all"  # every range of the window's tenant (every tenant's, when platform-wide)
    ranges = "ranges"  # the ranges in range_ids
    tags = "tags"  # ranges whose range template declares one of tags (top-level `tags:`)


class MaintenanceWindowIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500, description="Shown in every refusal and banner")
    starts_at: ZonedDateTime = Field(..., description="With a time zone offset, e.g. 2026-11-01T22:00:00-04:00")
    ends_at: ZonedDateTime = Field(..., description="With a time zone offset; the window is [starts_at, ends_at)")
    scope: WindowScope = WindowScope.all
    range_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    tags: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=50)
    platform_wide: bool = Field(
        False, description="Every tenant's ranges (platform administrator only). Fixed once created."
    )

    @field_validator("reason")
    @classmethod
    def _meaningful_reason(cls, v: str) -> str:
        v = v.strip()
        if len("".join(v.split())) < 3:
            raise ValueError("reason must have at least 3 characters besides spaces")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> MaintenanceWindowIn:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if self.scope is WindowScope.ranges and not self.range_ids:
            raise ValueError("scope 'ranges' needs at least one range in range_ids")
        if self.scope is WindowScope.tags and not [t for t in self.tags if t.strip()]:
            raise ValueError("scope 'tags' needs at least one tag in tags")
        if self.scope is not WindowScope.ranges and self.range_ids:
            raise ValueError("range_ids is only used with scope 'ranges'")
        if self.scope is not WindowScope.tags and self.tags:
            raise ValueError("tags is only used with scope 'tags'")
        return self


class MaintenanceWindowOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID | None = Field(None, description="None: platform-wide")
    platform_wide: bool
    reason: str
    starts_at: datetime
    ends_at: datetime
    scope: WindowScope
    range_ids: list[uuid.UUID]
    tags: list[str]
    active: bool = Field(..., description="In effect now")
    can_edit: bool = Field(..., description="Whether the caller may change or delete it")
    created_by: uuid.UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MaintenanceWindowListOut(BaseModel):
    items: list[MaintenanceWindowOut]
    total: int


class EmergencyTeardownIn(BaseModel):
    """A deliberate act: the caller repeats the range's id and says why."""

    confirm_range_id: uuid.UUID
    reason: str = Field(..., min_length=10, max_length=500, description="At least 10 characters besides spaces")

    @field_validator("reason")
    @classmethod
    def _meaningful(cls, v: str) -> str:
        v = v.strip()
        if len("".join(v.split())) < 10:
            raise ValueError("reason must have at least 10 characters besides spaces")
        return v
