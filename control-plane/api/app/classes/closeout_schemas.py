"""Attendance and close-out shapes (ADR 0014 §12-13; published in docs/interfaces/openapi.json)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from .schemas import has_control_chars


class AttendanceStatusOut(str, Enum):
    present = "present"
    absent = "absent"
    excused = "excused"


class AttendanceSourceOut(str, Enum):
    auto = "auto"
    instructor = "instructor"


class ResultOutcomeOut(str, Enum):
    passed = "pass"
    failed = "fail"
    incomplete = "incomplete"


def _plain(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if has_control_chars(value.replace("\n", " ")):
        raise ValueError("must not contain control characters")
    return value or None


class AttendanceSessionOut(BaseModel):
    session_id: uuid.UUID
    event_id: uuid.UUID
    name: str
    start_time: datetime
    end_time: datetime
    ended: bool = Field(description="Over: the clock records it (or has)")


class AttendanceCellOut(BaseModel):
    session_id: uuid.UUID
    status: AttendanceStatusOut | None = Field(
        None, description="None: not recorded yet (the session is under way, or the clock has not reached it)")
    source: AttendanceSourceOut | None = None
    evidence_at: datetime | None = Field(None, description="When the Student first opened their range in the session")
    note: str | None = None
    corrected_at: datetime | None = None


class AttendanceRowOut(BaseModel):
    entry_id: uuid.UUID
    email: str
    display_name: str | None = None
    roll_call_status: str
    cells: list[AttendanceCellOut]


class AttendanceGridOut(BaseModel):
    class_id: uuid.UUID
    class_status: str
    can_edit: bool
    sessions: list[AttendanceSessionOut]
    students: list[AttendanceRowOut]


class AttendanceCorrectionOut(str, Enum):
    """What an instructor may set (ADR 0014 §12): present or excused. Absent is the clock's
    finding, never a correction (#319)."""

    present = "present"
    excused = "excused"


class AttendanceCorrectIn(BaseModel):
    status: AttendanceCorrectionOut
    note: str | None = Field(None, max_length=500, description="Why it was corrected (audited)")

    @field_validator("note")
    @classmethod
    def _note(cls, v: str | None) -> str | None:
        return _plain(v)


class StandingOut(BaseModel):
    entry_id: uuid.UUID
    email: str
    display_name: str | None = None
    sessions_held: int
    present: int
    excused: int
    absent: int
    course_passed: bool | None = Field(None, description=(
        "Every graded module at or above its own pass threshold, unattempted ones failing; "
        "None: no graded result yet, or the course has no graded module"))
    course_percent: float | None = Field(None, description="Never on an Unclassified deployment")
    detections_credited: int | None = Field(None, description="Never on an Unclassified deployment")
    suggested: ResultOutcomeOut
    outcome: ResultOutcomeOut | None = Field(None, description="The result set at the last close-out")
    comment: str | None = None
    decided_at: datetime | None = None


class CloseoutOut(BaseModel):
    class_id: uuid.UUID
    class_status: str
    numbers_shown: bool = Field(description="False on an Unclassified deployment: pass/fail only")
    sessions_held: int
    closed_at: datetime | None = None
    students: list[StandingOut]


class ResultIn(BaseModel):
    entry_id: uuid.UUID
    outcome: ResultOutcomeOut
    comment: str | None = Field(None, max_length=1000)

    @field_validator("comment")
    @classmethod
    def _comment(cls, v: str | None) -> str | None:
        return _plain(v)


class CloseIn(BaseModel):
    results: list[ResultIn] = Field(default_factory=list, max_length=1000)


class ReopenIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        out = _plain(v)
        if not out or len(out) < 3:
            raise ValueError("give a reason")
        return out
