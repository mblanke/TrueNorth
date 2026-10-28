"""Wire shapes of the exercise template (kept out of ``app.schemas``, ADR 0003).

``ExerciseTemplateCore`` is the portable part of a template, strict (unknown fields are refused) and
bounded: the API validates it on write, and the content port validates the very same model on
a bundle's ``exercise_template`` content, so a bundle can carry nothing the API would refuse.
It holds no ids of this site: the scenario and the range template are references that the
API takes as ids and the bundle as item keys.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Side = Literal["blue", "red", "white", "green", "other"]
Delivery = Literal["cyber", "white_cell", "email", "radio", "physical", "opfor"]
ScalingKind = Literal["per_segment", "per_participants", "fixed"]
MAX_OBJECTIVES = 200
MAX_MESL = 1000
MAX_TEAMS = 20
MAX_ROLES = 40
MAX_RULES = 100
MAX_TEXT = 100_000
CORE_FIELDS = ("name", "description", "teams", "roles", "scaling_rules", "objectives", "mesl", "instructor_notes", "tags")


def _no_control(value: str) -> str:
    if any(ord(c) < 32 and c not in "\t\n\r" or ord(c) == 127 for c in value):
        raise ValueError("control characters are not allowed")
    return value


def _one_line(value: str) -> str:
    _no_control(value)
    if "\n" in value or "\r" in value or "\t" in value:
        raise ValueError("must be one line")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExerciseTeamSpec(_Strict):
    """A team the exercise needs. ``max_students`` 0: no limit."""

    name: str = Field(min_length=1, max_length=80)
    side: Side = "other"
    min_students: int = Field(0, ge=0, le=1000)
    max_students: int = Field(0, ge=0, le=1000)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _one_line(v)

    @model_validator(mode="after")
    def _range(self) -> ExerciseTeamSpec:
        if self.max_students and self.max_students < self.min_students:
            raise ValueError("max_students is below min_students")
        return self


class ExerciseVmSpec(_Strict):
    """What each participant of a role gets: ``count`` machines of a node type of the range
    template (matched against a node's id, type or os; the range template is the authority)."""

    node_type: str = Field(min_length=1, max_length=80)
    count: int = Field(1, ge=1, le=50)

    @field_validator("node_type")
    @classmethod
    def _line(cls, v: str) -> str:
        return _one_line(v)


class ExerciseRoleSpec(_Strict):
    """A participant role (analyst, red team, intel ...): how many Students may take it, how many
    by default, and the machines each one gets. ``max_participants`` 0: no limit. ``team``
    optionally names one of the template's teams. Sizing itself is not done here (Exercise Studio)."""

    name: str = Field(min_length=1, max_length=80)
    description: str = Field("", max_length=2000)
    team: str = Field("", max_length=80)
    min_participants: int = Field(0, ge=0, le=10_000)
    max_participants: int = Field(0, ge=0, le=10_000)
    default_count: int = Field(0, ge=0, le=10_000)
    vm_profile: list[ExerciseVmSpec] = Field(default_factory=list, max_length=20)

    @field_validator("name", "team")
    @classmethod
    def _line(cls, v: str) -> str:
        return _one_line(v)

    @field_validator("description")
    @classmethod
    def _text(cls, v: str) -> str:
        return _no_control(v)

    @model_validator(mode="after")
    def _sizes(self) -> ExerciseRoleSpec:
        if self.max_participants and self.max_participants < self.min_participants:
            raise ValueError(f"role {self.name}: max_participants is below min_participants")
        if self.default_count < self.min_participants or (self.max_participants and
                                                          self.default_count > self.max_participants):
            raise ValueError(f"role {self.name}: default_count is outside min_participants and max_participants")
        types = [v.node_type for v in self.vm_profile]
        if len(set(types)) != len(types):
            raise ValueError(f"role {self.name}: a node type is listed twice in its VM profile")
        return self


class ExerciseScalingRuleSpec(_Strict):
    """Shared infrastructure that scales with the exercise: ``count`` machines of ``node_type``
    per network segment (``per_segment``), per ``per`` participants (``per_participants``,
    steps of N; ``role`` limits the count to one role), or just once (``fixed``)."""

    name: str = Field(min_length=1, max_length=80)
    node_type: str = Field(min_length=1, max_length=80)
    kind: ScalingKind = "fixed"
    count: int = Field(1, ge=1, le=1000)
    per: int = Field(0, ge=0, le=10_000)
    role: str = Field("", max_length=80)
    segment: str = Field("", max_length=80)

    @field_validator("name", "node_type", "role", "segment")
    @classmethod
    def _line(cls, v: str) -> str:
        return _one_line(v)

    @model_validator(mode="after")
    def _shape(self) -> ExerciseScalingRuleSpec:
        if self.kind == "per_participants" and self.per < 1:
            raise ValueError(f"rule {self.name}: per_participants needs per (the step size) of at least 1")
        if self.kind != "per_participants" and (self.per or self.role):
            raise ValueError(f"rule {self.name}: per and role belong to per_participants rules only")
        if self.kind != "per_segment" and self.segment:
            raise ValueError(f"rule {self.name}: segment belongs to per_segment rules only")
        return self


class ExerciseObjectiveSpec(_Strict):
    ref: str = Field(min_length=1, max_length=32)
    text: str = Field("", max_length=20_000)
    moe: str = Field("", max_length=20_000)
    competency_code: str = Field("", max_length=50)

    @field_validator("ref", "competency_code")
    @classmethod
    def _line(cls, v: str) -> str:
        return _one_line(v)

    @field_validator("text", "moe")
    @classmethod
    def _text(cls, v: str) -> str:
        return _no_control(v)


class ExerciseMeslSpec(_Strict):
    """One serial of the MESL. Its delivery status is the exercise's, never the template's."""

    serial: int = Field(ge=0, le=100_000)
    phase: str = Field("", max_length=40)
    scenario_time: str = Field("", max_length=40)
    title: str = Field(min_length=1, max_length=255)
    description: str = Field("", max_length=20_000)
    objective_ref: str = Field("", max_length=32)
    attack_technique: str = Field("", max_length=32)
    delivery_method: Delivery = "cyber"
    from_cell: str = Field("", max_length=80)
    to_participant: str = Field("", max_length=80)
    expected_action: str = Field("", max_length=20_000)
    moe: str = Field("", max_length=20_000)

    @field_validator("phase", "scenario_time", "title", "objective_ref", "attack_technique", "from_cell",
                     "to_participant")
    @classmethod
    def _line(cls, v: str) -> str:
        return _one_line(v)

    @field_validator("description", "expected_action", "moe")
    @classmethod
    def _text(cls, v: str) -> str:
        return _no_control(v)


class ExerciseTemplateCore(_Strict):
    """What a template is, apart from where it lives: valid on its own."""

    name: str = Field(min_length=1, max_length=255)
    description: str = Field("", max_length=MAX_TEXT)
    teams: list[ExerciseTeamSpec] = Field(default_factory=list, max_length=MAX_TEAMS)
    roles: list[ExerciseRoleSpec] = Field(default_factory=list, max_length=MAX_ROLES)
    scaling_rules: list[ExerciseScalingRuleSpec] = Field(default_factory=list, max_length=MAX_RULES)
    objectives: list[ExerciseObjectiveSpec] = Field(default_factory=list, max_length=MAX_OBJECTIVES)
    mesl: list[ExerciseMeslSpec] = Field(default_factory=list, max_length=MAX_MESL)
    instructor_notes: str = Field("", max_length=MAX_TEXT)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _one_line(v)

    @field_validator("description", "instructor_notes")
    @classmethod
    def _text(cls, v: str) -> str:
        return _no_control(v)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        for tag in v:
            _one_line(tag)
            if not 1 <= len(tag) <= 60:
                raise ValueError("a tag is 1 to 60 characters")
        if len(set(v)) != len(v):
            raise ValueError("a tag is listed twice")
        return v

    @model_validator(mode="after")
    def _shape(self) -> ExerciseTemplateCore:
        teams = {t.name for t in self.teams}
        for r in self.roles:
            if r.team and r.team not in teams:
                raise ValueError(f"role {r.name} names team {r.team}, which the template lacks")
        roles = {r.name for r in self.roles}
        for rule in self.scaling_rules:
            if rule.role and rule.role not in roles:
                raise ValueError(f"rule {rule.name} names role {rule.role}, which the template lacks")
        for what, values in (("team", [t.name for t in self.teams]), ("role", [r.name for r in self.roles]),
                             ("scaling rule", [r.name for r in self.scaling_rules]), ("objective", [o.ref for o in self.objectives]),
                             ("MESL serial", [m.serial for m in self.mesl])):
            if len(set(values)) != len(values):
                raise ValueError(f"a {what} is listed twice")
        refs = {o.ref for o in self.objectives}
        for m in self.mesl:
            if m.objective_ref and m.objective_ref not in refs:
                raise ValueError(f"MESL serial {m.serial} names objective {m.objective_ref}, which the template lacks")
        return self


# ── the API ───────────────────────────────────────────────────────────────────────
class ExerciseTemplateIn(ExerciseTemplateCore):
    scenario_id: uuid.UUID | None = None
    range_template_id: uuid.UUID | None = None
    is_public: bool = Field(False, description="Share with every tenant (platform administrator only)")


class ExerciseTemplateUpdate(_Strict):
    """Only the fields sent change; an explicit null clears the scenario or range template."""

    name: str | None = None
    description: str | None = None
    teams: list[ExerciseTeamSpec] | None = None
    roles: list[ExerciseRoleSpec] | None = None
    scaling_rules: list[ExerciseScalingRuleSpec] | None = None
    objectives: list[ExerciseObjectiveSpec] | None = None
    mesl: list[ExerciseMeslSpec] | None = None
    instructor_notes: str | None = None
    tags: list[str] | None = None
    scenario_id: uuid.UUID | None = None
    range_template_id: uuid.UUID | None = None
    is_public: bool | None = None


class ExerciseTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    revision: int
    tenant_id: uuid.UUID | None = None
    is_public: bool
    shared: bool = Field(False, description="Another tenant's template, shared with this one: read-only here")
    scenario_id: uuid.UUID | None = None
    range_template_id: uuid.UUID | None = None
    teams: list[ExerciseTeamSpec]
    roles: list[ExerciseRoleSpec]
    scaling_rules: list[ExerciseScalingRuleSpec]
    objectives: list[ExerciseObjectiveSpec]
    mesl: list[ExerciseMeslSpec]
    instructor_notes: str
    tags: list[str]
    created_at: datetime
    updated_at: datetime


class ExerciseTemplateListOut(BaseModel):
    """A row of the list: counts, never the MESL or the notes (the exercise-control plan)."""

    id: uuid.UUID
    name: str
    description: str
    revision: int
    is_public: bool
    shared: bool
    scenario_id: uuid.UUID | None = None
    range_template_id: uuid.UUID | None = None
    team_names: list[str]
    role_names: list[str]
    objectives: int
    mesl_events: int
    tags: list[str]
    updated_at: datetime


class ExerciseFromTemplateIn(_Strict):
    name: str = Field(min_length=1, max_length=255)
    range_id: uuid.UUID
    max_score: int | None = Field(None, ge=0, le=1_000_000)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _one_line(v)


class ExerciseFromTemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    state: str
    range_id: uuid.UUID
    scenario_id: uuid.UUID | None = None
    max_score: int
    template_id: uuid.UUID
    template_revision: int
    objectives: int
    mesl_events: int


class ExerciseMadeFromTemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    state: str
    total_score: int
    max_score: int
    template_revision: int
    created_at: datetime


class ExerciseSaveAsTemplateIn(_Strict):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field("", max_length=MAX_TEXT)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _one_line(v)
