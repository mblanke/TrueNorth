"""Wire shapes of ``GET /ops/exercises/{id}/class`` (published in docs/interfaces/openapi.json)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ClassTeamOut(BaseModel):
    id: uuid.UUID
    name: str
    color_hex: str | None = None


class ClassLabOut(BaseModel):
    """The Student's own lab session on the exercise's range (the newest one)."""

    session_id: uuid.UUID
    state: str
    error: str = ""
    attempt: int = 1
    ready_at: datetime | None = None
    last_seen_at: datetime | None = None
    idle_expires_at: datetime | None = None
    max_expires_at: datetime | None = None


class ClassRangeOut(BaseModel):
    """The exercise's own range: a fault here is the exercise's, not any Student's."""

    id: uuid.UUID
    name: str
    state: str
    error: str | None = None


class ClassObjectiveOut(BaseModel):
    ref_id: str
    description: str = ""
    objective_type: str
    validator: str = ""
    points: int = 0


class ClassAttemptOut(BaseModel):
    """One detection attempt in this run. ``query`` and ``precision`` only for staff who may
    see the answer key (``scenario:update``), as ``GET /exercises/{id}/detections``."""

    id: uuid.UUID
    objective_ref: str
    verdict: str
    submitted_at: datetime | None = None
    events_matched: int | None = None
    query: str | None = None
    precision: float | None = None


class ClassObjectiveStateOut(BaseModel):
    """One objective as this Student stands on it, from their own attempts only."""

    ref_id: str
    state: Literal["credited", "pending", "missed", "not_attempted"]
    misses: int = 0
    credited_at: datetime | None = None


class AttentionOut(BaseModel):
    """Something an instructor may want to look at. Only what the data says today:

    - ``lab_fault``: the Student's lab is failed, expired or being cleaned up (not their doing)
    - ``lab_wait``: their lab is queued, building or resetting
    - ``learning``: ``learning_signal_misses`` or more judged misses on one objective, no credit
    - ``not_seen``: no detection and no lab heartbeat for ``not_seen_minutes`` (neutral)
    """

    kind: Literal["lab_fault", "lab_wait", "learning", "not_seen"]
    detail: str
    objective_ref: str | None = None
    since: datetime | None = None


class ClassStudentOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    team: ClassTeamOut | None = None
    lab: ClassLabOut | None = None
    attempts_total: int = Field(0, description="judged or pending attempts in this run")
    attempts_credited: int = 0
    credited_refs: list[str] = []
    score: int = Field(0, description="points of the objectives this Student's own detections were credited for")
    current_objective: str | None = Field(
        None, description="inferred: the objective of the latest unresolved attempt, else the first not credited"
    )
    last_detection_at: datetime | None = None
    last_activity_at: datetime | None = Field(None, description="latest of a detection or a lab heartbeat")
    objectives: list[ClassObjectiveStateOut] = []
    attempts: list[ClassAttemptOut] = []
    attention: list[AttentionOut] = []


class ExerciseClassOut(BaseModel):
    exercise_id: uuid.UUID
    exercise_name: str
    state: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    range: ClassRangeOut | None = None
    max_score: int = 0
    objectives: list[ClassObjectiveOut] = []
    students: list[ClassStudentOut] = []
    learning_signal_misses: int
    not_seen_minutes: int
    generated_at: datetime
