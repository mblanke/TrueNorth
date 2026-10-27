"""Wire schemas for the AD status sync panel (#290)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AdStatusSyncResultOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    reason: str = Field(description="ad_disabled | ad_removed | local_unused")
    reason_text: str
    applied: bool = Field(description="False in a dry run, a held tenant, a first sighting (confirmed by the next run), "
                                      "the last admin, or when someone else deactivated it first")
    seats_freed: int


class AdStatusSyncRunOut(BaseModel):
    id: uuid.UUID
    trigger: str = Field(description="nightly | manual")
    dry_run: bool
    status: str = Field(description="running | completed | held (this tenant) | failed | interrupted")
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    counts: dict[str, int] = Field(description="This tenant's counts only")
    results: list[AdStatusSyncResultOut] = Field(default_factory=list, description="This tenant's accounts only")


class AdStatusSyncOverviewOut(BaseModel):
    enabled: bool
    keycloak_configured: bool
    dry_run: bool = Field(description="AD_STATUS_SYNC_DRY_RUN: runs record and change nothing")
    nightly_hour_utc: int
    break_glass_usernames: list[str]
    last_break_glass_use: datetime | None = None
    latest: AdStatusSyncRunOut | None = None
    runs: list[AdStatusSyncRunOut] = Field(default_factory=list, description="Recent runs, newest first, without results")


class AdStatusSyncRunIn(BaseModel):
    dry_run: bool | None = Field(default=None, description="Omitted: the AD_STATUS_SYNC_DRY_RUN setting")
    override_limit: bool = Field(default=False, description="Apply even when over AD_STATUS_SYNC_MAX_FRACTION")
