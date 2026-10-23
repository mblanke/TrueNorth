"""Wire shapes of the content clock (ADR 0014 decisions 22-24). Kept out of ``app.schemas``
(ADR 0003)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from . import staleness

ItemType = Literal["course", "module", "lab", "scenario", "range_template", "golden_template"]
ClockState = Literal["green", "amber", "stale"]
ReasonKind = Literal["time", "release", "vulnerability", "upstream"]
_MIN, _MAX = staleness.MIN_CYCLE_DAYS, staleness.MAX_CYCLE_DAYS


class StaleReasonOut(BaseModel):
    kind: ReasonKind
    text: str
    origin_id: str = Field(description="The review item the staleness started at (this item for its own reasons)")
    source: str = ""
    at: str | None = None


class ContentClockOut(BaseModel):
    state: ClockState = Field(description="green: due in more than 14 days; amber: 14 or fewer; stale")
    days: int = Field(description="Whole days until the review is due (negative: overdue)")


class ContentItemBrief(BaseModel):
    id: uuid.UUID
    item_type: ItemType
    title: str
    stale: bool
    retired: bool


class ContentItemOut(BaseModel):
    id: uuid.UUID
    item_type: ItemType
    item_id: str = Field(description="The content's own id (course, template, golden template ...)")
    title: str
    shared: bool = Field(description="Shared catalogue content: every tenant reads it, a platform admin changes it")
    owner_id: uuid.UUID | None = None
    owner_name: str | None = None
    reviewer_id: uuid.UUID | None = None
    reviewer_name: str | None = None
    theme_ids: list[uuid.UUID] = []
    cycle_days: int = Field(description="The cycle in force (the item's own, else its type's)")
    cycle_override: int | None = Field(None, description="The item's own cycle, when it has one")
    last_reviewed_at: datetime | None = None
    last_reviewed_by_name: str | None = None
    due_at: datetime
    clock: ContentClockOut
    stale: bool
    stale_reasons: list[StaleReasonOut] = []
    retired: bool
    retired_at: datetime | None = None
    can_review: bool = Field(description="The caller may mark it reviewed (admin, owner or assigned reviewer)")
    can_assign: bool = Field(description="The caller may assign its reviewer (admin or owner)")
    can_admin: bool = Field(description="The caller may retire it and change its owner, themes and cycle")


class ContentItemDetailOut(ContentItemOut):
    depends_on: list[ContentItemBrief] = []
    used_by: list[ContentItemBrief] = []


class ContentItemListOut(BaseModel):
    items: list[ContentItemOut]
    theme_counts: dict[str, int] = Field(
        description="Items in the window per theme id, before the theme filter; 'none' counts untagged items")
    total: int


class MarkReviewedIn(BaseModel):
    note: str = Field("", max_length=500)


class AssignReviewerIn(BaseModel):
    reviewer_id: uuid.UUID | None = Field(None, description="An admin or instructor of the item's tenant; null clears")


class RetireIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ContentItemUpdateIn(BaseModel):
    """Admin only. Each field is changed only when it is sent."""

    owner_id: uuid.UUID | None = None
    theme_ids: list[uuid.UUID] | None = None
    cycle_days: int | None = Field(None, ge=_MIN, le=_MAX)
    clear_cycle: bool = Field(False, description="Go back to the content type's cycle")
    clear_owner: bool = False


class ThemeIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    colour: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$", description="#RRGGBB, the theme's dot")
    ordinal: int = Field(0, ge=0, le=1000)
    platform_wide: bool = Field(False, description="Every tenant's (platform administrator only)")


class ThemeOut(BaseModel):
    id: uuid.UUID
    name: str
    colour: str
    ordinal: int
    platform_wide: bool


class CycleIn(BaseModel):
    days: int = Field(ge=_MIN, le=_MAX)
    platform_wide: bool = Field(False, description="The default for every tenant (platform administrator only)")


class CycleOut(BaseModel):
    item_type: ItemType
    days: int
    source: Literal["tenant", "platform", "default"]
