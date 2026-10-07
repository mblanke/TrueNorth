"""Response shapes of the noise API (``app/routers/noise.py``).

They describe exactly what the handlers already returned as plain dicts, so declaring
them changed no response on the wire; it put the shapes into the published contract
(docs/interfaces/openapi.json) and from there into the console's generated types.
Request bodies stay in the router, next to their validation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

AgentState = Literal["pending", "ok", "lost"]


class NoisePresetsOut(BaseModel):
    """Named dial positions and the activity vocabulary."""

    presets: dict[str, int]
    activities: list[str]
    lookalikes: list[str]
    target_pools: list[str]


class NoiseDialOut(BaseModel):
    """What the effective level means: how much of the roster works, and how hard."""

    active_fraction: float
    actions_per_hour: float
    diurnal_amplitude: float
    lookalike_share: float


class NoiseProfileOut(BaseModel):
    """A range's noise settings. ``configured`` is false until anyone set them; the
    other fields are then the defaults a first PUT would start from."""

    range_id: str
    configured: bool
    enabled: bool
    paused: bool
    level: int
    effective_level: int
    seed: int
    utc_offset: int
    overrides: dict[str, int]
    targets: dict[str, list[str]]
    pack: str
    dial: NoiseDialOut


class NoiseAgentOut(BaseModel):
    """A registered agent. ``state`` is ``pending`` until its first poll, ``lost``
    after five missed polls."""

    id: str
    node: str
    zone: str | None = None
    version: str | None = None
    state: AgentState
    last_seen_at: str | None = None


class NoiseAgentIssuedOut(NoiseAgentOut):
    """A newly (re-)keyed agent. The token is shown this once and never again."""

    token: str


class NoisePersonaOut(BaseModel):
    id: str
    handle: str
    display_name: str
    title: str | None = None
    department: str | None = None
    node: str | None = None
    work_start: int | None = None
    work_end: int | None = None
    habits: dict[str, Any]
    lookalikes: bool | None = None
    attrs: dict[str, Any]


class NoisePlannedAction(BaseModel):
    """One action in a node's plan. ``lookalike`` is shown to the white cell only."""

    at: str
    persona: str
    kind: str
    target: str
    lookalike: bool
    params: dict[str, Any]


class NoisePlanOut(BaseModel):
    node: str
    level: int
    actions: list[NoisePlannedAction]


class NoiseActivityOut(BaseModel):
    """Ground truth: one action an agent reported having run."""

    at: str
    node: str
    persona: str | None = None
    kind: str
    target: str | None = None
    lookalike: bool
    ok: bool
    detail: dict[str, Any]


class NoiseStatsOut(BaseModel):
    minutes: int
    total: int
    failed: int
    lookalikes: int
    by_kind: dict[str, int]
    agents: dict[str, int]


class NoiseDeployAgent(BaseModel):
    node: str
    zone: str
    platform: str
    ip: str
    mgmt_ip: str


class NoiseDeploySkipped(BaseModel):
    node: str
    reason: str


class NoiseDeployOut(BaseModel):
    """What a deploy does (``dry_run``) or did. ``task_id`` only when it was handed to
    the worker."""

    dry_run: bool
    agents: list[NoiseDeployAgent]
    skipped: list[NoiseDeploySkipped]
    targets: dict[str, list[str]]
    dropped_targets: list[str]
    controller_url: str
    mgmt_cidr: str
    task_id: str | None = None


class NoiseAgentAction(BaseModel):
    """A planned action as an agent receives it: signed, and without ``lookalike``."""

    at: str
    persona: str
    kind: str
    target: str
    params: dict[str, Any]
    sig: str


class NoiseAgentPlanOut(BaseModel):
    node: str
    level: int
    poll_seconds: int
    actions: list[NoiseAgentAction]


class NoiseReportOut(BaseModel):
    accepted: int
    rejected: int
    duplicate: int
