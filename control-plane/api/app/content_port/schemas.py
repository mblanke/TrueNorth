"""Wire shapes of the content port: the signed bundle and the API (kept out of
``app.schemas``, ADR 0003).

The bundle models refuse anything they do not name (``extra="forbid"``) and bound every
list and string, so a signed bundle that is not exactly a content-port bundle is refused
before anything is read from it. Keys are ``<item_type>:<uuid>`` and nothing else: no item,
name or reference in a bundle is ever used as a path.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..exercise_templates.schemas import ExerciseTemplateCore

ItemType = Literal["course", "range_template", "scenario", "exercise_template"]
KEY_PATTERN = (r"^(course|range_template|scenario|exercise_template):"
               r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SCENARIO_KEY = r"^scenario:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
TEMPLATE_KEY = r"^range_template:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
HEX64 = r"^[0-9a-f]{64}$"
FORMAT = "truenorth.content-port.v1"
MAX_ITEMS = 500
MAX_YAML = 1024 * 1024  # safe_yaml.MAX_YAML_BYTES: what every reader of a template or scenario accepts
MAX_TEXT = 200_000


def _no_control(value: str) -> str:
    if any(ord(c) < 32 and c not in "\t\n\r" or ord(c) == 127 for c in value):
        raise ValueError("control characters are not allowed")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)


class _Name(_Strict):
    @field_validator("name", "title", "version", check_fields=False)
    @classmethod
    def _line(cls, v: str) -> str:
        _no_control(v)
        if "\n" in v or "\r" in v or "\t" in v:
            raise ValueError("must be one line")
        return v


# ── item contents ─────────────────────────────────────────────────────────────────
class TemplateContent(_Name):
    name: str = Field(min_length=1, max_length=255)
    version: str = Field("", max_length=50)
    yaml: str = Field(min_length=1, max_length=MAX_YAML)


class ScenarioContent(_Name):
    name: str = Field(min_length=1, max_length=255)
    version: str = Field("", max_length=50)
    yaml: str = Field(min_length=1, max_length=MAX_YAML)
    range_template: str | None = Field(None, pattern=TEMPLATE_KEY)


class ExerciseTemplateContent(ExerciseTemplateCore):
    """An exercise template (ADR 0016): the same model the API validates, plus the keys of the
    scenario and the range template it names. No tenant, owner, sharing or revision: the new
    site decides those."""

    scenario: str | None = Field(None, pattern=SCENARIO_KEY)
    range_template: str | None = Field(None, pattern=TEMPLATE_KEY)


class LessonContent(_Name):
    title: str = Field(min_length=1, max_length=255)
    body_markdown: str = Field("", max_length=MAX_TEXT)
    duration_minutes: int = Field(0, ge=0, le=100_000)
    competency_codes: str = Field("[]", max_length=10_000)


class QuestionContent(_Strict):
    ordinal: int = Field(ge=0, le=10_000)
    question_type: Literal["mcq", "multi", "truefalse", "scenario"]
    stem: str = Field(min_length=1, max_length=20_000)
    options: str = Field("[]", max_length=50_000)
    correct: str = Field("[]", max_length=10_000)
    explanation: str = Field("", max_length=20_000)
    competency_code: str = Field("", max_length=50)
    difficulty: str = Field("intermediate", max_length=20)
    points: int = Field(10, ge=0, le=100_000)


class QuizContent(_Name):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field("", max_length=MAX_TEXT)
    pass_pct: int = Field(70, ge=0, le=100)
    time_limit_minutes: int = Field(0, ge=0, le=100_000)
    shuffle_questions: bool = True
    max_attempts: int = Field(0, ge=0, le=1000)
    questions: list[QuestionContent] = Field(default_factory=list, max_length=1000)


class ModuleContentItem(_Strict):
    ordinal: int = Field(ge=0, le=10_000)
    content_kind: Literal["teach", "check", "assess"]
    scenario: str | None = Field(None, pattern=SCENARIO_KEY)
    external_ref: str = Field("", max_length=20_000)
    lesson: LessonContent | None = None
    quiz: QuizContent | None = None


class PoRef(_Strict):
    qsp_code: str = Field(min_length=1, max_length=32)
    po_code: str = Field(min_length=1, max_length=32)


class ModuleItem(_Name):
    ordinal: int = Field(ge=0, le=10_000)
    title: str = Field(min_length=1, max_length=255)
    description: str = Field("", max_length=MAX_TEXT)
    content_type: Literal["scenario", "external_lti", "reading", "quiz", "video"]
    duration_minutes: int = Field(0, ge=0, le=100_000)
    is_required: bool = True
    pass_threshold: int = Field(70, ge=0, le=100)
    po: PoRef | None = None
    scenario: str | None = Field(None, pattern=SCENARIO_KEY)
    content_ref: str = Field("", max_length=MAX_TEXT, description="The authored module content, without its "
                                                                  "scenario id (carried as ``scenario``)")
    contents: list[ModuleContentItem] = Field(default_factory=list, max_length=100)


class CourseContent(_Name):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field("", max_length=MAX_TEXT)
    version: str = Field("", max_length=50)
    difficulty: str = Field("intermediate", max_length=50)
    duration_hours: int = Field(0, ge=0, le=100_000)
    tags: str = Field("", max_length=10_000)
    nice_work_roles: str = Field("", max_length=10_000)
    course_meta: str = Field("{}", max_length=MAX_TEXT)
    qsp_code: str | None = Field(None, max_length=32)
    modules: list[ModuleItem] = Field(default_factory=list, max_length=500)


CONTENT_MODELS: dict[str, type[BaseModel]] = {
    "range_template": TemplateContent, "scenario": ScenarioContent, "course": CourseContent,
    "exercise_template": ExerciseTemplateContent}


# ── proof and builds ──────────────────────────────────────────────────────────────
class ProofEvidence(_Strict):
    kind: Literal["certification", "class", "exercise", "review"]
    id: str = Field(max_length=64)
    at: datetime | None = None


class ProofCondition(_Strict):
    name: Literal["certified", "used", "fresh"]
    met: bool
    not_applicable: bool = False
    evidence: list[ProofEvidence] = Field(default_factory=list, max_length=20)


class ProofSummary(_Strict):
    proven: bool
    conditions: list[ProofCondition] = Field(max_length=3)


class PortGoldenBuild(_Strict):
    catalogue_id: str = Field(min_length=1, max_length=64)
    build_id: str = Field(min_length=1, max_length=120)
    checksum: str = Field("", max_length=128)


class BundleItem(_Strict):
    key: str = Field(pattern=KEY_PATTERN)
    item_type: ItemType
    origin_id: uuid.UUID
    content_sha256: str = Field(pattern=HEX64)
    content: dict
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    proof: ProofSummary
    golden_builds: list[PortGoldenBuild] = Field(default_factory=list, max_length=200)
    removed_passwords: int = Field(0, ge=0, le=10_000)


class BundleOrigin(_Strict):
    key_id: str = Field(pattern=r"^[0-9a-f]{16}$")


class BundleIn(_Strict):
    """A content-port bundle without its signature."""

    format: Literal["truenorth.content-port.v1"]
    bundle_id: uuid.UUID
    exported_at: datetime
    origin: BundleOrigin
    items: list[BundleItem] = Field(min_length=1, max_length=MAX_ITEMS)
    golden_builds: list[PortGoldenBuild] = Field(default_factory=list, max_length=2000)


# ── the API ───────────────────────────────────────────────────────────────────────
class ProvenEvidenceOut(BaseModel):
    kind: Literal["certification", "class", "exercise", "review"]
    id: str
    label: str
    at: datetime | None = None


class ProvenConditionOut(BaseModel):
    name: Literal["certified", "used", "fresh"]
    title: str
    met: bool
    not_applicable: bool = False
    reasons: list[str] = []
    evidence: list[ProvenEvidenceOut] = []


class ProvenDependencyOut(BaseModel):
    item_type: ItemType
    item_id: str
    title: str | None = Field(None, description="None: missing here, deleted, or outside the item's scope")
    proven: bool | None = Field(None, description="None when the dependency could not be read")


class PortImportedFromOut(BaseModel):
    origin_key_id: str
    exported_at: datetime
    imported_at: datetime
    proven_at_origin: bool
    golden_builds: list[PortGoldenBuild] = []


class ProvenStatusOut(BaseModel):
    item_type: ItemType
    item_id: str
    title: str
    proven: bool = Field(description="All three conditions hold on this site")
    label: str = Field(description='"Proven", or "Not proven: <reasons>"')
    reasons: list[str] = []
    conditions: list[ProvenConditionOut]
    dependencies: list[ProvenDependencyOut] = []
    golden_builds: list[PortGoldenBuild] = []
    imported: PortImportedFromOut | None = Field(None, description="Ported here from another site; not proven here "
                                                               "until this site's own evidence says so")


class PortExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PortItemRef] = Field(min_length=1, max_length=MAX_ITEMS)


class PortItemRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_type: ItemType
    item_id: uuid.UUID


class PortSigningKeyOut(BaseModel):
    alg: str
    key_id: str
    public_key: str = Field(description="Base64 raw Ed25519 public key: put it in tn_content_port_trusted_keys on "
                                        "the importing site")


Action = Literal["create", "update", "unchanged", "conflict", "refused", "skipped"]
Conflict = Literal["local_changed", "local_name", "older", "visibility"]


class PortPreviewItemOut(BaseModel):
    key: str
    item_type: ItemType
    name: str
    action: Action
    conflict: Conflict | None = None
    local_id: str | None = None
    reasons: list[str] = Field(default_factory=list, description="Why it is a conflict, refused or skipped")
    notes: list[str] = Field(default_factory=list, description="What the import changes or leaves for you to do")
    diff: str = Field("", description="Unified diff, local to incoming (truncated)")
    proven_at_origin: bool = True


class PortMissingBuildOut(BaseModel):
    catalogue_id: str
    build_id: str
    checksum: str = ""
    needed_by: list[str]


class PortPreviewOut(BaseModel):
    bundle_id: uuid.UUID
    exported_at: datetime
    key_id: str
    signed_sha256: str
    target_tenant_id: uuid.UUID
    shared: bool
    already_imported_at: datetime | None = Field(None, description="This bundle was imported before: refused")
    items: list[PortPreviewItemOut]
    missing_golden_builds: list[PortMissingBuildOut] = Field(
        default_factory=list, description="Port these templates first (OVF export plus build record, S2 promotion)")
    can_import: bool


class PortImportOut(BaseModel):
    import_id: uuid.UUID
    bundle_id: uuid.UUID
    created: int
    updated: int
    unchanged: int
    skipped: int
    items: list[PortPreviewItemOut]
    missing_golden_builds: list[PortMissingBuildOut] = []


PortExportIn.model_rebuild()
