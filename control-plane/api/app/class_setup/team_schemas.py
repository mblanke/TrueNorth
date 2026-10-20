"""Team request and response schemas (routers/admin.py), moved out of app/schemas.py (ADR 0003).

``TeamFullIn.exercise_id`` (#200): the exercise a team is for. An instructor creates a team
only for an exercise they run (app/class_setup/scope.py); without it the team is a standing
one, created by an administrator.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TeamFullIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    team_type: str | None = Field(default=None, pattern=r"^(red|blue|white|purple|green|orange|custom)$")
    color_hex: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    ou_id: uuid.UUID | None = None
    nation_id: uuid.UUID | None = None
    max_members: int | None = None
    is_persistent: bool = True
    exercise_id: uuid.UUID | None = Field(
        default=None,
        description="The exercise the team is for (your tenant's). Required for an instructor: one they run.",
    )


class TeamUpdate(BaseModel):
    name: str = Field(default=None)
    description: str | None = None
    team_type: str | None = None
    color_hex: str | None = None
    max_members: int | None = None


class TeamFullOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    tenant_id: uuid.UUID | None = None
    description: str | None = None
    team_type: str | None = None
    color_hex: str | None = None
    ou_id: uuid.UUID | None = None
    nation_id: uuid.UUID | None = None
    max_members: int | None = None
    is_persistent: bool
    exercise_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
