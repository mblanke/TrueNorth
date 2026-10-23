"""Class and roll-call request and response shapes (published in docs/interfaces/openapi.json)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

# What one paste or CSV may add. A class is 12-15 Students; a whole course intake fits.
MAX_ROLL_CALL_BATCH = 500


class ClassStatusOut(str, Enum):
    open = "open"
    cancelled = "cancelled"


class RollCallStatusOut(str, Enum):
    invited = "invited"
    signed_in = "signed_in"
    ready = "ready"
    removed = "removed"


class ClassRole(str, Enum):
    """The caller's part in a class, as the API decides it."""

    lead = "lead"
    assistant = "assistant"
    observer = "observer"
    admin = "admin"
    student = "student"


class ClassIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=120, description="Name or serial, e.g. C201-26-03")
    course_id: uuid.UUID
    start_date: date
    end_date: date
    lead_instructor_id: uuid.UUID | None = Field(
        None, description="An instructor or admin of the tenant. Default: the caller (an instructor)"
    )
    assistant_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    observer_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("name must be at least 2 characters")
        return v

    @model_validator(mode="after")
    def _dates(self) -> ClassIn:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class ClassPerson(BaseModel):
    id: uuid.UUID
    display_name: str
    email: str


class ClassOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    course_id: uuid.UUID
    course_name: str
    start_date: date
    end_date: date
    status: ClassStatusOut
    lead_instructor: ClassPerson | None
    assistants: list[ClassPerson]
    observers: list[ClassPerson]
    my_role: ClassRole | None = Field(..., description="None once the caller has handed the class over")
    can_edit: bool = Field(..., description="The caller is its lead or an admin: may change it and its roll call")
    roll_call_count: int = Field(..., description="Entries not removed")
    created_at: datetime | None = None


class RollCallEntryOut(BaseModel):
    id: uuid.UUID
    email: str
    user_id: uuid.UUID | None
    display_name: str | None
    status: RollCallStatusOut
    onboarding_sent_at: datetime | None
    reminded_at: datetime | None
    signed_in_at: datetime | None
    removed_at: datetime | None
    created_at: datetime | None = None


class RollCallAddIn(BaseModel):
    """Emails to add, as a list, as pasted text (any mix of commas, semicolons, spaces and
    new lines; "Name <email>" works), or both."""

    emails: list[str] = Field(default_factory=list, max_length=MAX_ROLL_CALL_BATCH)
    text: str = Field("", max_length=200_000)


class RollCallRefusal(BaseModel):
    email: str
    reason: str


class RollCallAddOut(BaseModel):
    added: list[RollCallEntryOut]
    already_on_roll_call: list[str]
    invalid: list[str] = Field(..., description="Pieces of the input that are not an email address")
    refused: list[RollCallRefusal]
