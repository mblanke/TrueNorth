"""Unit readiness shapes (ADR 0014 decision 7; published in docs/interfaces/openapi.json).

Pass, fail or incomplete and the instructor's comment: never a score, a percentage or a
letter grade, on any deployment."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from .closeout_schemas import ResultOutcomeOut


class ReadinessStudentOut(BaseModel):
    entry_id: uuid.UUID
    email: str
    display_name: str | None = Field(None, description="None: on the roll call, never signed in")
    unit: str | None = Field(None, description="The Student's unit (users.unit); None when unknown")
    roll_call_status: str
    enrolment_status: str | None = Field(None, description="Their enrolment on the class's course, if any")
    present: int = Field(description="Sessions recorded present")
    excused: int
    absent: int
    outcome: ResultOutcomeOut | None = Field(None, description="The close-out result; None: not decided yet")
    comment: str | None = Field(None, description="The instructor's comment on the result")
    decided_at: datetime | None = None
    qualification_earned: str | None = Field(
        None, description="The course's qualification (NQual), when the result is a pass")


class ReadinessClassOut(BaseModel):
    class_id: uuid.UUID
    name: str
    course_id: uuid.UUID
    course_name: str
    qualification: str | None = Field(None, description="The NQual the course delivers, if any")
    start_date: date
    end_date: date
    status: str
    closed_at: datetime | None = None
    sessions_held: int = Field(description="Approved sessions that have ended")
    enrolled: int = Field(description="Students on the roll call shown (forfeited seats included)")
    passed: int
    failed: int
    incomplete: int
    awaiting_result: int
    students: list[ReadinessStudentOut]


class ReadinessQualificationOut(BaseModel):
    qualification_id: uuid.UUID
    code: str = Field(description="The NQual granted")
    title: str
    earned: int = Field(description="Passes, in the classes shown, on courses that deliver it")


class ReadinessSummaryOut(BaseModel):
    classes: int
    enrolments: int = Field(description="Places on the classes shown: one per Student per class")
    students: int = Field(description="Different people")
    sessions_held: int
    present: int
    excused: int
    absent: int
    passed: int
    failed: int
    incomplete: int
    awaiting_result: int
    qualifications_earned: int


class UnitReadinessOut(BaseModel):
    date_from: date
    date_to: date
    unit: str | None = Field(None, description="The unit filter applied; None: every unit")
    units: list[str] = Field(description="The units of the Students on the classes in the date range")
    summary: ReadinessSummaryOut
    classes: list[ReadinessClassOut]
    qualifications: list[ReadinessQualificationOut]
