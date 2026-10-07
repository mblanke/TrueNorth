"""Response shapes for the ARC² Course Studio API (routers/arc2_studio.py).

The top level of each response is typed for the published contract. Parts that mirror
the engine's own files (outline, objectives, findings, human actions, gate records) stay
open-ended: the engine's manifest schema (tools/arc2/manifest.schema.json) owns them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class StageView(BaseModel):
    key: str
    name: str
    state: str
    stop_reason: Any = None


class JobView(BaseModel):
    id: str | None = None
    action: str | None = None
    state: str | None = None
    current_agent: str | None = None
    error: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class RunSummary(BaseModel):
    slug: str
    name: str
    title: str | None = None
    code: str | None = None
    request: str | None = None
    phase: str
    phase_text: str
    stages: list[StageView]
    gates: dict[str, dict[str, Any]]
    qa: dict[str, Any]
    actions_open: int
    actions_blocking: int
    job: JobView | None = None
    updated_at: str | None = None


class RunList(BaseModel):
    runs: list[RunSummary]
    runner_seen: str | None = None


class RunDetail(RunSummary):
    messages: list[dict[str, Any]]
    outline: Any = None
    objectives: list[Any]
    modules: list[dict[str, Any]]
    pages: list[str]
    lab: dict[str, Any]
    findings: list[Any]
    human_actions: list[Any]
    files: list[dict[str, Any]]
    package_ready: bool


class RunFile(BaseModel):
    path: str
    text: str
    instructor_only: bool
