"""API shapes for scenario runs (kept out of app/schemas.py, ADR 0003)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

InjectStatus = Literal["fired", "failed", "skipped"]
TimelineStatus = Literal["pending", "fired", "failed", "skipped"]


class InjectRecordOut(BaseModel):
    """One recorded inject outcome."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str = Field(description="timeline | instructor")
    run_id: str | None = Field(None, description="the run that fired it; a replay is a new run")
    seq: int | None = Field(None, description="timeline position; null for an instructor inject")
    t: str | None = None
    action: str
    status: InjectStatus
    detail: str = ""
    execution_mode: str | None = Field(None, description="simulated (synthetic records) | live; null if not run")
    mitre_technique: str | None = None
    telemetry_count: int = 0
    telemetry_shipped: bool = False
    created_at: datetime | None = None


class ScenarioExecuteIn(BaseModel):
    scenario_id: uuid.UUID
    range_id: uuid.UUID


class ScenarioExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scenario_id: uuid.UUID | None
    scenario_name: str
    range_id: uuid.UUID | None
    state: Literal["pending", "running", "completed", "failed"]
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime | None = None


class TimelineEntryOut(BaseModel):
    """A timeline event and what happened to it (``pending`` until the worker records it)."""

    seq: int
    t: str | None = None
    action: str
    status: TimelineStatus
    detail: str = ""
    execution_mode: str | None = None
    mitre_technique: str | None = None
    telemetry_count: int = 0
    recorded_at: datetime | None = None


class ObjectiveResultOut(BaseModel):
    ref_id: str
    description: str = ""
    status: Literal["unassessed"] = Field(
        "unassessed", description="an execution has no Students or evidence review, so nothing is scored"
    )


class InjectCounts(BaseModel):
    total: int
    fired: int
    skipped: int
    failed: int
    pending: int


class ScenarioExecutionResultsOut(ScenarioExecutionOut):
    injects: InjectCounts
    objectives: list[ObjectiveResultOut]
