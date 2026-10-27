"""Class and roll-call request and response shapes (published in docs/interfaces/openapi.json)."""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def has_control_chars(value: str) -> bool:
    """Any character of Unicode category C (CR, LF, NUL, format characters...)."""
    # C*: control and format characters (CR, LF, NUL...); Zl/Zp: the Unicode line and
    # paragraph separators, which mail software may also treat as line breaks.
    return any(unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in ("Zl", "Zp") for ch in value)


# What one paste or CSV may add. A class is 12-15 Students; a whole course intake fits.
MAX_ROLL_CALL_BATCH = 500


class ClassStatusOut(str, Enum):
    open = "open"
    cancelled = "cancelled"
    closed = "closed"  # closed out (ADR 0014 §13): read-only until an admin reopens it


class RollCallStatusOut(str, Enum):
    invited = "invited"
    signed_in = "signed_in"
    ready = "ready"
    removed = "removed"
    pending = "pending"  # added after approval with no room: waits for an approver (ADR 0014 §17)
    forfeited = "forfeited"  # not signed in by 2 hours after the start: seat lost (ADR 0014 §4)


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
        # The name goes into email subjects: no line breaks or other control characters.
        if has_control_chars(v):
            raise ValueError("name must not contain line breaks or control characters")
        return v

    @model_validator(mode="after")
    def _dates(self) -> ClassIn:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class ClassPerson(BaseModel):
    id: uuid.UUID
    display_name: str
    email: str | None = Field(None, description="Omitted when the caller is a Student")


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
    can_edit: bool = Field(..., description="The caller is its lead or an admin and the class is open: may change it and its roll call")
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
    pending_reason: str | None = Field(None, description="Why an addition waits for an approver, or was rejected")
    forfeited_at: datetime | None = Field(
        None, description="When they lost their seat for not signing in in time; kept after a reinstatement"
    )
    forfeit_deadline: datetime | None = Field(
        None, description="A forfeited Student: the time by which they had to sign in (2 hours after the start)"
    )


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
    not_emailed: list[RollCallRefusal] = Field(
        default_factory=list, description="Added, but the onboarding email was not sent (and why)"
    )
    pending_approval: list[RollCallRefusal] = Field(
        default_factory=list,
        description="Not added yet: the class's approved sessions have no room for them, so an approver "
        "decides (ADR 0014 §17). The reason says what does not fit",
    )


class PendingAdditionOut(BaseModel):
    """A roll-call addition waiting for an approver (ADR 0014 §17): only what a capacity
    decision needs. Approvers (range ops among them) need not be on the class, so no Student
    is named here; the class's own people see who it is on its roll call."""

    entry_id: uuid.UUID
    class_id: uuid.UUID
    class_name: str
    seats: int = Field(1, description="Seats the addition needs in each approved session")
    roll_call_count: int = Field(0, description="Students on the class's roll call now")
    reason: str | None
    created_at: datetime | None


class AdditionDecisionOut(BaseModel):
    """What an approver's decision on an addition did, without naming the Student."""

    entry_id: uuid.UUID
    class_id: uuid.UUID
    status: RollCallStatusOut
    emailed: bool = Field(False, description="An onboarding email went to them")
    not_emailed: str | None = Field(None, description="Why no onboarding email went, if one was due")


class ReinstateOut(BaseModel):
    """What reinstating a Student who lost their seat did (ADR 0014 §4)."""

    entry_id: uuid.UUID
    class_id: uuid.UUID
    status: RollCallStatusOut = Field(
        ..., description="signed_in or invited when their seat was taken again; pending when it waits for an approver"
    )
    pending_reason: str | None = Field(None, description="What did not fit, when it waits for an approver")


class AdditionRejectIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000)


class RollCallDomainsIn(BaseModel):
    email_domains: list[str] = Field(..., max_length=100, description="e.g. forces.gc.ca; empty: any domain")

    @field_validator("email_domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for d in v:
            d = d.strip().lower().lstrip("@")
            if not _DOMAIN.match(d):
                raise ValueError(f"not a domain: {d[:80]!r}")
            if d not in out:
                out.append(d)
        return out


class RollCallDomainsOut(BaseModel):
    email_domains: list[str]
    source: str = Field(..., description="'setting' (set by an admin) or 'accounts' (the tenant's own accounts)")
