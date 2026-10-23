"""Class ranges, session modes and teams: request and response shapes (ADR 0014 §9-11; #282).

Published in docs/interfaces/openapi.json (ADR 0002). Nothing here carries a credential: a
Student reads their own seat's through ``/ranges/{range_id}/credentials`` (ADR 0013 §3).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class RangeModeOut(str, Enum):
    individual = "individual"
    collective = "collective"


class CourseRangeModeIn(BaseModel):
    mode: RangeModeOut


class CourseRangeModeOut(BaseModel):
    course_id: uuid.UUID
    mode: RangeModeOut
    source: str = Field(..., description="'setting' (set for this organisation) or 'default' (collective)")


class ClassRangeStatus(str, Enum):
    pending = "pending"  # not built yet (before the lead, or waiting: e.g. a maintenance window)
    building = "building"
    ready = "ready"
    failed = "failed"
    tearing_down = "tearing_down"
    torn_down = "torn_down"


class ClassRangeRowOut(BaseModel):
    id: uuid.UUID
    kind: str = Field(..., description="seat: one Student's range (individual sessions); shared: the class's one")
    template_id: uuid.UUID
    entry_id: uuid.UUID | None = Field(None, description="The seat's roll-call entry")
    email: str | None = Field(None, description="The seat's Student; staff only")
    display_name: str | None = None
    range_id: uuid.UUID | None
    status: ClassRangeStatus
    error: str | None = Field(None, description="Why its build failed or waits; staff only")
    builds: int = 0
    built_at: datetime | None = None
    torn_down_at: datetime | None = None
    teardown_reason: str | None = None


class ClassSessionModeOut(BaseModel):
    event_id: uuid.UUID
    name: str
    state: str
    start_time: datetime
    end_time: datetime
    range_mode: RangeModeOut
    template_id: uuid.UUID | None = None
    range_id: uuid.UUID | None = None


class ClassRangesOut(BaseModel):
    """A class's sessions with their mode, and its ranges with their status. Staff see every
    seat; a Student only their own seat and the shared ranges."""

    sessions: list[ClassSessionModeOut]
    ranges: list[ClassRangeRowOut]
    failed: int = Field(0, description="Ranges whose build failed or could not be started")


class TeamSplitIn(BaseModel):
    team_count: int = Field(..., ge=2, le=26)


class TeamMoveIn(BaseModel):
    team_id: uuid.UUID


class SessionTeamMember(BaseModel):
    user_id: uuid.UUID
    display_name: str


class SessionTeamOut(BaseModel):
    id: uuid.UUID
    name: str
    members: list[SessionTeamMember]


class UnassignedOut(BaseModel):
    email: str = Field(..., description="On the roll call with no account yet: placed once they sign in")


class SessionTeamsOut(BaseModel):
    event_id: uuid.UUID
    editable: bool = Field(..., description="The caller may change them now (lead or admin, before the start)")
    teams: list[SessionTeamOut]
    unassigned: list[UnassignedOut]
