"""First-run onboarding shapes (moved out of ``schemas.py``, ADR 0003; published in
docs/interfaces/openapi.json)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class OnboardingStateOut(BaseModel):
    state: str
    steps_done: list[str] = []
    missing_profile_fields: list[str] = []
    qualification_id: uuid.UUID | None = None
    learning_path_id: uuid.UUID | None = None
    enrolled_course_count: int = 0
    onboarded_at: datetime | None = None
    via_roll_call: bool = Field(
        False,
        description="Admitted from a class roll call on first sign-in (ADR 0014 §1): there was no access "
        "request, so the wizard skips the step that confirms one",
    )


class OnboardingProfileIn(BaseModel):
    """Self-service profile completion.

    Deliberately excludes role, tenant_id, clearance_level and is_active — a
    trainee may describe themselves, never promote themselves.
    """

    rank: str | None = None
    service_branch: str | None = None
    unit: str | None = None
    callsign: str | None = None
    nation_id: uuid.UUID | None = None
    timezone: str | None = None


class OnboardingPathIn(BaseModel):
    qualification_id: uuid.UUID | None = None
    learning_path_id: uuid.UUID | None = None
