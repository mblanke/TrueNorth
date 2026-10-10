"""API shapes for Moodle results (kept out of app/schemas.py, ADR 0003)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MoodleResultsPullIn(BaseModel):
    reset: bool = False  # start again from the beginning; recording is idempotent


class MoodleResultsStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    platform_id: uuid.UUID
    cursor: str = ""
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str = ""
    rows_seen: int = 0
    rows_applied: int = 0
    running: bool = False


class MoodleResultsPullOut(BaseModel):
    platform_id: uuid.UUID
    cursor: str
    pages: int
    rows: int
    applied: int
    unchanged: int
    skipped: dict[str, int]
    more: bool
