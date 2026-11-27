# Author: Marc-Andre Blanke
# Created: 2026-11-27T00:00:00Z
"""Shapes of the Students page (``student_progress``; published in docs/interfaces/openapi.json).

As on the readiness page (ADR 0014): a class result is pass, fail or incomplete with the
instructor's comment, never a score, a percentage or a grade. Attendance is counts of
sessions. Proficiency is a NICE level name (novice .. expert), from competency assertions.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from .closeout_schemas import ResultOutcomeOut


class StudentRowOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    email: str
    rank: str | None = None
    unit: str | None = Field(None, description="The formation (users.unit); None when unknown")
    callsign: str | None = None
    proficiency: str | None = Field(
        None, description="Overall NICE proficiency level from their competency assertions; None: none recorded"
    )
    classes: int = Field(description="Classes they are on (cancelled ones left out)")
    present: int = Field(description="Sessions recorded present, every class")
    excused: int
    absent: int
    passed: int
    failed: int
    incomplete: int
    awaiting_result: int = Field(description="Classes with no result yet")
    qualifications: list[str] = Field(description="NQuals earned (a pass on a closed class, enrolment completed)")


class StudentListOut(BaseModel):
    units: list[str] = Field(description="Every formation of the organisation's Students, one spelling each")
    students: list[StudentRowOut]


class StudentCompetencyOut(BaseModel):
    category: str = Field(description="A NICE category")
    proficiency: str = Field(description="Their level in it, from the assertions in that category")
    assertions: int


class StudentClassOut(BaseModel):
    class_id: uuid.UUID
    name: str
    course_name: str
    qualification: str | None = Field(None, description="The NQual the course delivers, if any")
    start_date: date
    end_date: date
    status: str
    sessions_held: int
    present: int
    excused: int
    absent: int
    outcome: ResultOutcomeOut | None = Field(None, description="The close-out result; None: not decided yet")
    comment: str | None = Field(None, description="The instructor's comment on the result")
    decided_at: datetime | None = None
    qualification_earned: str | None = None


class StudentRecordOut(StudentRowOut):
    competencies: list[StudentCompetencyOut] = Field(description="By NICE category, in the framework's order")
    class_history: list[StudentClassOut] = Field(description="Newest class first")
