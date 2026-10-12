"""API shapes for Moodle results (kept out of app/schemas.py, ADR 0003)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

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
    # The scheduled pull's interval (MOODLE_RESULTS_PULL_SECONDS; 0 = off), and whether the
    # results are stale: never pulled successfully, or not within two intervals.
    scheduled_every_seconds: float = 0
    stale: bool = True
    stale_reason: str = ""


class MoodleSyncStatusOut(BaseModel):
    """How fresh one Moodle's results are in TrueNorth (service.sync_status)."""

    platform_id: uuid.UUID
    platform_name: str
    state: Literal["current", "stale", "failing", "never"]
    stale: bool
    last_success_at: datetime | None = None
    last_run_at: datetime | None = None
    last_error: str = ""  # staff only; empty for a Student
    sync_interval_seconds: int = 0  # 0: no scheduled pull, only "Pull now"
    running: bool = False


class MoodleResultOut(BaseModel):
    """One fact Moodle reported for this enrolment (a quiz grade or an activity completion)."""

    platform_id: uuid.UUID
    kind: str  # "quiz_grade" | "completion"
    activity: str  # the Moodle activity's TrueNorth idnumber, tn:<module>:<slot>
    release_id: uuid.UUID | None = None
    grade: float | None = None
    grade_max: float | None = None
    completion_state: int | None = None  # Moodle's: 0 incomplete, 1 complete, 2 pass, 3 fail
    complete: bool | None = None
    source_time: datetime | None = None  # when Moodle wrote it (its timemodified)
    source_attempt_id: int | None = None  # Moodle's quiz_attempts.id behind a grade
    quiz_attempt_id: uuid.UUID | None = None  # the TrueNorth attempt mirroring the grade
    recorded_at: datetime | None = None  # when TrueNorth last recorded it


class MoodleEnrolmentResultsOut(BaseModel):
    course_id: uuid.UUID
    user_id: uuid.UUID
    enrollment_id: uuid.UUID | None = None
    enrollment_status: str | None = None
    sync: list[MoodleSyncStatusOut]
    results: list[MoodleResultOut]


class MoodleResultsPullOut(BaseModel):
    platform_id: uuid.UUID
    cursor: str
    pages: int
    rows: int
    applied: int
    unchanged: int
    skipped: dict[str, int]
    more: bool
