"""A Student's "My classes" page: response shapes (ADR 0014 §20; issue #284).

Published in docs/interfaces/openapi.json (ADR 0002). Nothing here names another Student,
carries a staff email, or carries a credential: Join opens ``/ranges/{range_id}/credentials``,
which decides again (ADR 0013 §3).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field

from .range_schemas import RangeModeOut


class AttendingSeatStatus(str, Enum):
    ready = "ready"  # on the roll call
    forfeited = "forfeited"  # lost to the forfeit rule (ADR 0014 §4); the lead can reinstate


class AttendingRangeStatus(str, Enum):
    not_built = "not_built"  # not built yet, or no longer standing
    building = "building"
    ready = "ready"
    failed = "failed"


class AttendingSessionOut(BaseModel):
    event_id: uuid.UUID
    name: str
    start_time: datetime
    end_time: datetime
    range_mode: RangeModeOut = Field(..., description="individual: your own range; collective: one shared range")
    live: bool = Field(..., description="Under way now")
    range_status: AttendingRangeStatus | None = Field(None, description="Your range for this session; None: no range")


class AttendingClassOut(BaseModel):
    id: uuid.UUID
    name: str
    course_name: str
    lead_instructor: str | None = Field(None, description="The lead instructor's name (never an email)")
    start_date: date
    end_date: date
    onboarded: bool = Field(..., description="You have signed in and taken your place on the roll call")
    seat: AttendingSeatStatus
    forfeit_deadline: datetime | None = Field(None, description="When the seat was lost: the sign-in deadline missed")
    forfeited_at: datetime | None = None
    sessions: list[AttendingSessionOut] = Field(..., description="Its sessions in the window asked for, earliest first")
    range_status: AttendingRangeStatus | None = Field(
        None, description="Your range for the current or next session; None: no session to come uses a range"
    )
    next_session_id: uuid.UUID | None = Field(None, description="The current or next session")
    join_range_id: uuid.UUID | None = Field(
        None,
        description="Set only when you may open the range now: your own seat, ready; or the class's shared range, "
        "ready, during a collective session. Its credentials are at /ranges/{range_id}/credentials",
    )


class AttendingClassesOut(BaseModel):
    acknowledgement_signed: bool = Field(..., description="You have signed the current rules of use")
    window_start: datetime
    window_end: datetime
    classes: list[AttendingClassOut]
