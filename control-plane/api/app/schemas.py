"""TrueNorth Range — Pydantic v2 request/response schemas."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


# ── Health ─────────────────────────────────────────────────────────────
class HealthOut(BaseModel):
    status: str
    version: str
    app: str = "TrueNorth Range"
    db: bool = False
    redis: bool = False


# ── Tenants ────────────────────────────────────────────────────────────
class TenantIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime


# ── Users ──────────────────────────────────────────────────────────────
class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    display_name: str
    role: str
    tenant_id: uuid.UUID
    is_active: bool
    created_at: datetime


# ── Teams ──────────────────────────────────────────────────────────────
class TeamIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class TeamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    tenant_id: uuid.UUID | None = None
    created_at: datetime


# ── Templates ──────────────────────────────────────────────────────────
class TemplateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    version: str = Field(default="1.0", max_length=50)
    yaml: str = Field(..., min_length=1)
    is_public: bool = False


class TemplateUpdate(BaseModel):
    name: str | None = None
    version: str | None = None
    yaml: str | None = None
    is_public: bool | None = None


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    version: str
    yaml: str
    is_public: bool
    tenant_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class TemplateListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    version: str
    is_public: bool
    created_at: datetime


# ── Scenarios ──────────────────────────────────────────────────────────
class ScenarioIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    version: str = Field(default="1.0", max_length=50)
    yaml: str = Field(..., min_length=1)
    is_public: bool = False


class ScenarioUpdate(BaseModel):
    name: str | None = None
    version: str | None = None
    yaml: str | None = None
    is_public: bool | None = None


class ScenarioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    version: str
    yaml: str
    is_public: bool
    tenant_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class ScenarioListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    version: str
    is_public: bool
    created_at: datetime


# ── Ranges ─────────────────────────────────────────────────────────────
class RangeIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    template_id: uuid.UUID


class RangeUpdate(BaseModel):
    name: str | None = None


class RangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    state: str
    template_id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    provisioner_backend: str | None = None
    provisioner_output: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class RangeListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    state: str
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


# ── Range Snapshots ────────────────────────────────────────────────────
class SnapshotIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    range_id: uuid.UUID
    name: str
    description: str | None = None
    snapshot_state: str
    size_bytes: int
    range_state_at_snapshot: str
    tenant_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ── Exercises ──────────────────────────────────────────────────────────
class ExerciseIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    range_id: uuid.UUID
    scenario_id: uuid.UUID
    max_score: int = Field(default=100, ge=0)


class ExerciseUpdate(BaseModel):
    name: str | None = None
    max_score: int | None = None


class ExerciseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    state: str
    range_id: uuid.UUID
    scenario_id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    total_score: int
    max_score: int
    created_at: datetime
    updated_at: datetime


class ExerciseListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    state: str
    total_score: int
    max_score: int
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


# ── Objectives ─────────────────────────────────────────────────────────
class ObjectiveOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    exercise_id: uuid.UUID
    ref_id: str
    objective_type: str
    description: str
    validator: str
    points: int
    achieved: bool
    evidence: str | None = None
    achieved_at: datetime | None = None


class ObjectiveAck(BaseModel):
    evidence: str | None = None


# ── AAR ────────────────────────────────────────────────────────────────
class AAROut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    exercise_id: uuid.UUID
    report_json: str
    report_html: str | None = None
    generated_at: datetime


# ── Audit Log ──────────────────────────────────────────────────────────
class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    timestamp: datetime
    user_id: uuid.UUID | None = None
    action: str
    resource_type: str
    resource_id: str
    detail: str | None = None


# ── Pagination ─────────────────────────────────────────────────────────
class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


# -- Cursor-based Pagination (for 70k+ record sets) --------------------
class CursorPage(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int


# -- Batch Operations (70k-VM scale) -----------------------------------
class BatchProvisionIn(BaseModel):
    range_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=500)


class BatchProvisionOut(BaseModel):
    dispatched: int
    task_id: str


class RangeStatsOut(BaseModel):
    total_ranges: int = 0
    by_state: dict[str, int] = Field(default_factory=dict)
    total_vms: int = 0
    active_exercises: int = 0


# ══════════════════════════════════════════════════════════════════════════
# LMS Schemas
# ══════════════════════════════════════════════════════════════════════════


# ── Courses ────────────────────────────────────────────────────────────
class CourseModuleIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    content_type: str = Field(..., pattern=r"^(scenario|external_lti|reading|quiz|video)$")
    content_ref: str = ""
    duration_minutes: int = Field(default=0, ge=0)
    is_required: bool = True
    pass_threshold: int = Field(default=70, ge=0, le=100)


class CourseModuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    course_id: uuid.UUID
    ordinal: int
    title: str
    description: str
    content_type: str
    content_ref: str
    duration_minutes: int
    is_required: bool
    pass_threshold: int


class CourseIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    version: str = Field(default="1.0", max_length=50)
    difficulty: str = Field(default="intermediate", pattern=r"^(beginner|intermediate|advanced)$")
    duration_hours: int = Field(default=0, ge=0)
    tags: list[str] = Field(default_factory=list)
    nice_work_roles: list[str] = Field(default_factory=list)
    is_published: bool = False
    modules: list[CourseModuleIn] = Field(default_factory=list)


class CourseUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    version: str | None = None
    difficulty: str | None = None
    duration_hours: int | None = None
    tags: list[str] | None = None
    nice_work_roles: list[str] | None = None
    is_published: bool | None = None


class CourseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: str
    version: str
    difficulty: str
    duration_hours: int
    tags: str  # JSON string from DB
    nice_work_roles: str
    is_published: bool
    tenant_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    modules: list[CourseModuleOut] = Field(default_factory=list)


class CourseListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: str
    version: str
    difficulty: str
    duration_hours: int
    is_published: bool
    created_at: datetime


# ── Enrollments ────────────────────────────────────────────────────────
class EnrollmentIn(BaseModel):
    user_id: uuid.UUID
    course_id: uuid.UUID


class EnrollmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    user_id: uuid.UUID
    course_id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    status: str
    enrolled_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    final_grade: str | None = None
    final_score: int
    max_score: int
    created_at: datetime


class ModuleProgressOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    enrollment_id: uuid.UUID
    module_id: uuid.UUID
    status: str
    score: int
    max_score: int
    attempts: int
    last_accessed_at: datetime | None = None
    completed_at: datetime | None = None


# ── Learning Paths ─────────────────────────────────────────────────────
class LearningPathIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    course_ids: list[uuid.UUID] = Field(default_factory=list)
    is_published: bool = False


class LearningPathUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    course_ids: list[uuid.UUID] | None = None
    is_published: bool | None = None


class LearningPathOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: str
    course_ids: list[str]
    is_published: bool
    tenant_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("course_ids", mode="before")
    @classmethod
    def _parse_course_ids(cls, v):
        # Stored as a JSON string in the Text column; expose a real list.
        if isinstance(v, str):
            try:
                parsed = json.loads(v or "[]")
                return parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                return []
        return v or []


# ── Competencies ───────────────────────────────────────────────────────
class CompetencyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    name: str
    description: str
    framework: str
    category: str
    level: str


class CompetencyAssertionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    user_id: uuid.UUID
    competency_id: uuid.UUID
    proficiency: str
    evidence_refs: str
    assessed_at: datetime
    source: str


class CompetencyProfileOut(BaseModel):
    """Aggregated competency profile for a user across all platforms."""

    user_id: uuid.UUID
    assertions: list[CompetencyAssertionOut] = Field(default_factory=list)
    total_competencies: int = 0
    by_framework: dict[str, int] = Field(default_factory=dict)
    by_proficiency: dict[str, int] = Field(default_factory=dict)


class SkillGapOut(BaseModel):
    """Competencies required by target role but not yet demonstrated by user."""

    competency: CompetencyOut
    required_level: str
    current_level: str | None = None
    gap: bool = True
    recommended_courses: list[uuid.UUID] = Field(default_factory=list)


# ── Certifications ─────────────────────────────────────────────────────
class CertificationIn(BaseModel):
    cert_name: str = Field(..., min_length=1, max_length=255)
    issuer: str = Field(..., min_length=1, max_length=255)
    credential_id: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    verification_url: str | None = None
    nice_work_roles: list[str] = Field(default_factory=list)
    dod_8140_category: str | None = None


class CertificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    user_id: uuid.UUID
    cert_name: str
    issuer: str
    credential_id: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    verification_url: str | None = None
    status: str
    nice_work_roles: str
    dod_8140_category: str | None = None
    created_at: datetime


# ── External Platforms ─────────────────────────────────────────────────
class ExternalPlatformIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100)
    platform_type: str = Field(..., pattern=r"^(moodle|immersive_labs|offsec|custom)$")
    base_url: str = Field(..., min_length=1)
    auth_type: str = Field(..., pattern=r"^(lti13|oauth2|api_key|saml)$")
    lti_client_id: str | None = None
    lti_deployment_id: str | None = None
    lti_issuer: str | None = None
    lti_jwks_url: str | None = None
    lti_token_url: str | None = None


class ExternalPlatformUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    auth_type: str | None = None
    is_active: bool | None = None
    lti_client_id: str | None = None
    lti_deployment_id: str | None = None
    lti_issuer: str | None = None
    lti_jwks_url: str | None = None
    lti_token_url: str | None = None


class ExternalPlatformOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    platform_type: str
    base_url: str
    auth_type: str
    is_active: bool
    lti_client_id: str | None = None
    lti_deployment_id: str | None = None
    lti_issuer: str | None = None
    lti_jwks_url: str | None = None
    lti_token_url: str | None = None
    tenant_id: uuid.UUID
    last_sync_at: datetime | None = None
    created_at: datetime


class ExternalActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    platform_id: uuid.UUID
    user_id: uuid.UUID
    external_ref: str
    activity_type: str
    title: str
    description: str
    score: int | None = None
    max_score: int | None = None
    passed: bool | None = None
    completed_at: datetime | None = None
    duration_seconds: int | None = None


# ── Transcript ─────────────────────────────────────────────────────────
class TranscriptEntry(BaseModel):
    """A single learning record in a user's unified transcript."""

    source: str  # truenorth / moodle / immersive_labs / offsec
    activity_type: str  # exercise / course / lab / certification
    title: str
    score: int | None = None
    max_score: int | None = None
    grade: str | None = None
    completed_at: datetime | None = None
    competencies_earned: list[str] = Field(default_factory=list)


class TranscriptOut(BaseModel):
    user_id: uuid.UUID
    entries: list[TranscriptEntry] = Field(default_factory=list)
    total_entries: int = 0
    total_hours: float = 0.0
    certifications: list[CertificationOut] = Field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════
# Infrastructure Schemas
# ══════════════════════════════════════════════════════════════════════════


# ── Hypervisor Connections ─────────────────────────────────────────────
class HypervisorConnectionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    hypervisor_type: str = Field(..., pattern=r"^(proxmox|vsphere|hyperv)$")
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(default=8006, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=255)
    password: str | None = None
    api_token: str | None = None
    verify_ssl: bool = False
    is_primary: bool = False
    datacenter: str | None = None
    notes: str | None = None


class HypervisorConnectionUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    api_token: str | None = None
    verify_ssl: bool | None = None
    is_primary: bool | None = None
    is_active: bool | None = None
    datacenter: str | None = None
    notes: str | None = None


class HypervisorConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    hypervisor_type: str
    host: str
    port: int
    username: str
    verify_ssl: bool
    is_primary: bool
    is_active: bool
    datacenter: str | None = None
    notes: str | None = None
    tenant_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class HypervisorNodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    connection_id: uuid.UUID
    node_name: str
    ip_address: str | None = None
    status: str
    cpu_total: int | None = None
    cpu_used: float | None = None
    memory_total_gb: float | None = None
    memory_used_gb: float | None = None
    storage_total_gb: float | None = None
    storage_used_gb: float | None = None
    vm_count: int
    last_seen_at: datetime | None = None


class HypervisorPoolOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    connection_id: uuid.UUID
    pool_name: str
    pool_type: str
    total_capacity_gb: float | None = None
    used_capacity_gb: float | None = None
    is_default: bool


class HypervisorTestResult(BaseModel):
    success: bool
    message: str
    version: str | None = None
    nodes_found: int = 0


class HypervisorSummaryOut(BaseModel):
    total_connections: int = 0
    active_connections: int = 0
    total_nodes: int = 0
    online_nodes: int = 0
    total_vms: int = 0
    total_cpu: int = 0
    total_memory_gb: float = 0.0
    total_storage_gb: float = 0.0
    by_type: dict[str, int] = Field(default_factory=dict)


# ── AI Backend Config ──────────────────────────────────────────────────
class AIBackendConfigIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    backend_type: str = Field(..., pattern=r"^(ollama|openai|anthropic|azure_openai|mock)$")
    base_url: str = Field(..., min_length=1)
    api_key: str | None = None
    is_primary: bool = False
    max_concurrent: int = Field(default=10, ge=1)
    timeout_seconds: int = Field(default=120, ge=5)
    notes: str | None = None


class AIBackendConfigUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    is_active: bool | None = None
    is_primary: bool | None = None
    max_concurrent: int | None = None
    timeout_seconds: int | None = None
    notes: str | None = None


class AIBackendConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    backend_type: str
    base_url: str
    is_active: bool
    is_primary: bool
    max_concurrent: int
    timeout_seconds: int
    notes: str | None = None
    tenant_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class AIFleetNodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    backend_id: uuid.UUID
    node_name: str
    url: str
    status: str
    gpu_model: str | None = None
    gpu_vram_gb: float | None = None
    loaded_models: str | None = None
    current_requests: int
    max_requests: int
    last_health_check: datetime | None = None


class AIModelRouteIn(BaseModel):
    model_pattern: str = Field(..., min_length=1, max_length=255)
    backend_id: uuid.UUID
    priority: int = Field(default=0, ge=0)
    tags: str | None = None


class AIModelRouteUpdate(BaseModel):
    model_pattern: str | None = None
    backend_id: uuid.UUID | None = None
    priority: int | None = None
    tags: str | None = None
    is_active: bool | None = None


class AIModelRouteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    model_pattern: str
    backend_id: uuid.UUID
    priority: int
    tags: str | None = None
    is_active: bool
    created_at: datetime


class AIFleetSummaryOut(BaseModel):
    total_backends: int = 0
    active_backends: int = 0
    total_nodes: int = 0
    online_nodes: int = 0
    total_gpu_vram_gb: float = 0.0
    active_requests: int = 0
    model_routes: int = 0
    by_backend_type: dict[str, int] = Field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════
# Directory & Nations Schemas
# ══════════════════════════════════════════════════════════════════════════


class NationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    iso_alpha2: str
    iso_alpha3: str
    flag_emoji: str
    is_nato: bool
    is_fvey: bool
    is_active: bool


class CoalitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    description: str | None = None
    is_active: bool
    created_at: datetime


class CoalitionMembershipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    nation_id: uuid.UUID
    coalition_id: uuid.UUID


class OUIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100)
    ou_type: str = Field(default="custom", pattern=r"^(country|branch|division|unit|section|custom)$")
    parent_id: uuid.UUID | None = None
    nation_id: uuid.UUID | None = None


class OUUpdate(BaseModel):
    name: str | None = None
    slug: str | None = None
    ou_type: str | None = None
    parent_id: uuid.UUID | None = None


class OUOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    ou_type: str
    parent_id: uuid.UUID | None = None
    dn_path: str | None = None
    nation_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    created_at: datetime


class OUTreeOut(BaseModel):
    """OU with nested children for tree rendering."""

    id: uuid.UUID
    name: str
    slug: str
    ou_type: str
    parent_id: uuid.UUID | None = None
    nation_id: uuid.UUID | None = None
    children: list[OUTreeOut] = Field(default_factory=list)


class SecurityGroupIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100)
    group_type: str = Field(default="access", pattern=r"^(access|distribution|training|role_based)$")
    description: str | None = None
    ou_id: uuid.UUID | None = None


class SecurityGroupUpdate(BaseModel):
    name: str | None = None
    slug: str | None = None
    group_type: str | None = None
    description: str | None = None


class SecurityGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    group_type: str
    description: str | None = None
    ou_id: uuid.UUID | None = None
    ad_object_guid: str | None = None
    tenant_id: uuid.UUID | None = None
    created_at: datetime


class SecurityGroupMembershipIn(BaseModel):
    user_id: uuid.UUID
    group_id: uuid.UUID


# ── Extended User schemas ──────────────────────────────────────────────
class UserFullOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    keycloak_id: str
    email: str
    display_name: str
    role: str
    tenant_id: uuid.UUID
    is_active: bool
    first_name: str | None = None
    last_name: str | None = None
    rank: str | None = None
    service_branch: str | None = None
    nation_id: uuid.UUID | None = None
    clearance_level: str
    unit: str | None = None
    callsign: str | None = None
    avatar_url: str | None = None
    source: str
    ad_object_guid: str | None = None
    auth_method_preference: str | None = None
    timezone: str
    created_at: datetime
    updated_at: datetime


class UserUpdateIn(BaseModel):
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    rank: str | None = None
    service_branch: str | None = None
    nation_id: uuid.UUID | None = None
    clearance_level: str | None = None
    unit: str | None = None
    callsign: str | None = None
    avatar_url: str | None = None
    role: str | None = None
    is_active: bool | None = None
    auth_method_preference: str | None = None
    timezone: str | None = None


class UserCreateIn(BaseModel):
    email: str = Field(..., min_length=1, max_length=320)
    display_name: str = Field(..., min_length=1, max_length=255)
    keycloak_id: str = Field(default="", max_length=255)
    role: str = Field(default="student", pattern=r"^(admin|instructor|student|observer|range_ops)$")
    first_name: str | None = None
    last_name: str | None = None
    rank: str | None = None
    service_branch: str | None = None
    nation_id: uuid.UUID | None = None
    clearance_level: str = "unclassified"
    unit: str | None = None
    callsign: str | None = None


# ── Extended Team schemas ──────────────────────────────────────────────
class TeamFullIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    team_type: str | None = Field(default=None, pattern=r"^(red|blue|white|purple|green|orange|custom)$")
    color_hex: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    ou_id: uuid.UUID | None = None
    nation_id: uuid.UUID | None = None
    max_members: int | None = None
    is_persistent: bool = True


class TeamUpdate(BaseModel):
    name: str | None = None
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


# ── Auth Zone Policies ─────────────────────────────────────────────────
class AuthZonePolicyIn(BaseModel):
    zone_name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    allowed_methods: str = "password_mfa,fido2"
    require_mfa: bool = True
    session_timeout_minutes: int = Field(default=480, ge=5)
    max_failed_attempts: int = Field(default=5, ge=1)
    ip_whitelist: str | None = None
    clearance_required: str = "unclassified"


class AuthZonePolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    zone_name: str
    description: str | None = None
    allowed_methods: str
    require_mfa: bool
    session_timeout_minutes: int
    max_failed_attempts: int
    ip_whitelist: str | None = None
    clearance_required: str
    is_active: bool
    tenant_id: uuid.UUID | None = None
    created_at: datetime


# ── AD Sync ────────────────────────────────────────────────────────────
class ADSyncStatusOut(BaseModel):
    connected: bool = False
    last_sync_at: datetime | None = None
    users_synced: int = 0
    groups_synced: int = 0
    errors: list[str] = Field(default_factory=list)


class ADSyncTriggerOut(BaseModel):
    task_id: str
    message: str


# ── Storage Schemas ────────────────────────────────────────────────────
class StorageApplianceIn(BaseModel):
    name: str
    vendor: str
    model: str
    management_ip: str
    protocol: str = "nfs"
    raw_capacity_tb: float = 0
    usable_capacity_tb: float = 0
    notes: str | None = None


class StorageApplianceUpdate(BaseModel):
    name: str | None = None
    vendor: str | None = None
    model: str | None = None
    management_ip: str | None = None
    protocol: str | None = None
    raw_capacity_tb: float | None = None
    usable_capacity_tb: float | None = None
    is_active: bool | None = None
    notes: str | None = None


class StorageApplianceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    vendor: str
    model: str
    management_ip: str
    protocol: str
    raw_capacity_tb: float
    usable_capacity_tb: float
    is_active: bool
    notes: str | None = None
    created_at: datetime


class StorageVolumeIn(BaseModel):
    appliance_id: uuid.UUID
    volume_name: str
    size_gb: float = 0
    protocol: str = "nfs"
    mount_path: str | None = None


class StorageVolumeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    appliance_id: uuid.UUID
    volume_name: str
    size_gb: float
    used_gb: float
    protocol: str
    mount_path: str | None = None
    created_at: datetime


class StorageSummaryOut(BaseModel):
    total_appliances: int = 0
    active_appliances: int = 0
    total_raw_tb: float = 0
    total_usable_tb: float = 0
    total_volumes: int = 0


# ── Network Device Schemas ─────────────────────────────────────────────
class NetworkDeviceIn(BaseModel):
    name: str
    vendor: str
    model: str
    role: str = "tor"
    management_ip: str
    firmware_version: str | None = None
    port_count: int = 0
    notes: str | None = None


class NetworkDeviceUpdate(BaseModel):
    name: str | None = None
    vendor: str | None = None
    model: str | None = None
    role: str | None = None
    management_ip: str | None = None
    firmware_version: str | None = None
    port_count: int | None = None
    is_active: bool | None = None
    notes: str | None = None


class NetworkDeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    vendor: str
    model: str
    role: str
    management_ip: str
    firmware_version: str | None = None
    port_count: int
    is_active: bool
    notes: str | None = None
    created_at: datetime


class NetworkSummaryOut(BaseModel):
    total_devices: int = 0
    active_devices: int = 0
    by_role: dict[str, int] = Field(default_factory=dict)


# ── Kit Schemas ────────────────────────────────────────────────────────
class KitDefinitionIn(BaseModel):
    name: str
    slug: str
    description: str | None = None


class KitDefinitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    description: str | None = None
    compute_node_count: int
    storage_appliance_count: int
    network_device_count: int
    created_at: datetime


# ══════════════════════════════════════════════════════════════════════════
# Helpdesk / Tickets
# ══════════════════════════════════════════════════════════════════════════


class SupportQueueIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    description: str | None = None
    is_default: bool = False


class SupportQueueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    description: str | None = None
    is_default: bool
    created_at: datetime


class SupportQueueMemberIn(BaseModel):
    user_id: uuid.UUID
    role: str = "agent"


class SupportQueueMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    queue_id: uuid.UUID
    user_id: uuid.UUID
    role: str


class TicketIn(BaseModel):
    subject: str = Field(..., min_length=1, max_length=500)
    description: str = Field(..., min_length=1)
    priority: str = "medium"
    category: str = "other"
    queue_id: uuid.UUID | None = None
    range_id: uuid.UUID | None = None
    exercise_id: uuid.UUID | None = None


class TicketUpdate(BaseModel):
    subject: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    category: str | None = None
    assignee_id: uuid.UUID | None = None
    queue_id: uuid.UUID | None = None


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    subject: str
    description: str
    status: str
    priority: str
    category: str
    tenant_id: uuid.UUID | None = None
    reporter_id: uuid.UUID
    assignee_id: uuid.UUID | None = None
    queue_id: uuid.UUID | None = None
    range_id: uuid.UUID | None = None
    exercise_id: uuid.UUID | None = None
    ai_triaged: bool
    ai_confidence: float | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TicketListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    subject: str
    status: str
    priority: str
    category: str
    reporter_id: uuid.UUID
    assignee_id: uuid.UUID | None = None
    queue_id: uuid.UUID | None = None
    ai_triaged: bool
    created_at: datetime
    updated_at: datetime


class TicketCommentIn(BaseModel):
    body: str = Field(..., min_length=1)
    is_internal: bool = False


class TicketCommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    ticket_id: uuid.UUID
    author_id: uuid.UUID
    body: str
    is_internal: bool
    comment_source: str
    created_at: datetime


class AIAgentActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    ticket_id: uuid.UUID
    action_type: str
    input_summary: str | None = None
    output_summary: str | None = None
    confidence: float | None = None
    model_used: str | None = None
    tokens_used: int | None = None
    latency_ms: int | None = None
    was_applied: bool
    created_at: datetime


# ══════════════════════════════════════════════════════════════════════════
# Wiki / Knowledge Base
# ══════════════════════════════════════════════════════════════════════════


class WikiSpaceIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    description: str | None = None
    icon: str = "folder"


class WikiSpaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    description: str | None = None
    icon: str
    is_archived: bool
    created_at: datetime


class WikiSpaceListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    icon: str
    is_archived: bool
    created_at: datetime


class WikiPageIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    body: str = ""
    parent_id: uuid.UUID | None = None
    tags: str | None = None
    is_published: bool = True


class WikiPageUpdate(BaseModel):
    title: str | None = None
    body: str | None = None
    parent_id: uuid.UUID | None = None
    tags: str | None = None
    is_published: bool | None = None
    ordinal: int | None = None
    edit_summary: str | None = None


class WikiPageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    space_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    title: str
    slug: str
    body: str
    author_id: uuid.UUID
    last_editor_id: uuid.UUID
    ordinal: int
    is_published: bool
    tags: str | None = None
    created_at: datetime
    updated_at: datetime


class WikiPageListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    space_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    title: str
    slug: str
    ordinal: int
    is_published: bool
    tags: str | None = None
    created_at: datetime
    updated_at: datetime


class WikiPageTreeNode(BaseModel):
    id: uuid.UUID
    title: str
    slug: str
    children: list[WikiPageTreeNode] = Field(default_factory=list)


class WikiRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    page_id: uuid.UUID
    revision_number: int
    title: str
    body: str
    editor_id: uuid.UUID
    edit_summary: str | None = None
    created_at: datetime


# ── Threat Intelligence ────────────────────────────────────────────────
class ThreatIntelFeedIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    feed_type: str = Field(..., pattern=r"^(taxii|stix_file|custom_api)$")
    url: str | None = Field(None, max_length=2048)
    collection_id: str | None = Field(None, max_length=255)
    api_key_ref: str | None = Field(None, max_length=255)
    poll_interval_minutes: int = Field(60, ge=5, le=10080)
    is_enabled: bool = True


class ThreatIntelFeedUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    url: str | None = Field(None, max_length=2048)
    collection_id: str | None = None
    api_key_ref: str | None = None
    poll_interval_minutes: int | None = Field(None, ge=5, le=10080)
    is_enabled: bool | None = None


class ThreatIntelFeedOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    feed_type: str
    url: str | None = None
    collection_id: str | None = None
    poll_interval_minutes: int
    is_enabled: bool
    last_poll_at: datetime | None = None
    last_poll_status: str | None = None
    indicator_count: int
    tenant_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class ThreatIndicatorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    feed_id: uuid.UUID
    stix_id: str | None = None
    indicator_type: str
    value: str
    name: str | None = None
    description: str | None = None
    confidence: int
    severity: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    kill_chain_phases: str | None = None
    mitre_attack_ids: str | None = None
    is_active: bool
    created_at: datetime


class ThreatIndicatorSearch(BaseModel):
    indicator_type: str | None = None
    value: str | None = None
    severity: str | None = None
    min_confidence: int | None = Field(None, ge=0, le=100)
    feed_id: uuid.UUID | None = None


# ── Detection Rules (Sigma) ───────────────────────────────────────────
class DetectionRuleIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    sigma_id: str | None = Field(None, max_length=100)
    status: str = Field("draft", pattern=r"^(draft|testing|stable|deprecated)$")
    description: str | None = None
    author: str | None = Field(None, max_length=255)
    level: str = Field("medium", pattern=r"^(informational|low|medium|high|critical)$")
    logsource_category: str | None = Field(None, max_length=100)
    logsource_product: str | None = Field(None, max_length=100)
    logsource_service: str | None = Field(None, max_length=100)
    detection_yaml: str = Field(..., min_length=10)
    mitre_attack_ids: list[str] | None = None
    false_positives: list[str] | None = None
    tags: list[str] | None = None
    is_enabled: bool = True


class DetectionRuleUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=500)
    status: str | None = Field(None, pattern=r"^(draft|testing|stable|deprecated)$")
    description: str | None = None
    level: str | None = Field(None, pattern=r"^(informational|low|medium|high|critical)$")
    logsource_category: str | None = None
    logsource_product: str | None = None
    logsource_service: str | None = None
    detection_yaml: str | None = Field(None, min_length=10)
    mitre_attack_ids: list[str] | None = None
    false_positives: list[str] | None = None
    tags: list[str] | None = None
    is_enabled: bool | None = None


class DetectionRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    sigma_id: str | None = None
    status: str
    description: str | None = None
    author: str | None = None
    level: str
    logsource_category: str | None = None
    logsource_product: str | None = None
    logsource_service: str | None = None
    detection_yaml: str
    mitre_attack_ids: str | None = None
    false_positives: str | None = None
    tags: str | None = None
    is_enabled: bool
    tenant_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class SigmaValidationResult(BaseModel):
    valid: bool
    errors: list[str] = []
    warnings: list[str] = []


# ── Exercise Forge (EPIC 1) ─────────────────────────────────────────────


class ForgeIndicatorIn(BaseModel):
    indicator_id: uuid.UUID | None = None
    indicator_type: str = Field(..., description="ipv4, domain, sha256, etc.")
    value: str
    severity: str = "medium"
    mitre_attack_ids: list[str] = []
    description: str | None = None


class ForgeRequest(BaseModel):
    feed_id: uuid.UUID | None = Field(None, description="Pull indicators from this feed")
    indicators: list[ForgeIndicatorIn] = Field(
        default_factory=list, description="Manual indicators (used if feed_id is None)"
    )
    # Curriculum Forge mode: generate from learning objectives instead of threat intel
    curriculum_id: uuid.UUID | None = Field(
        None, description="Ground the scenario in this curriculum's RAG index"
    )
    learning_objectives: list[str] = Field(
        default_factory=list,
        max_length=15,
        description="Learning objectives the exercise must assess (curriculum mode)",
    )
    difficulty: str = Field(default="intermediate", pattern=r"^(beginner|intermediate|advanced|expert)$")
    duration_minutes: int = Field(default=60, ge=15, le=480)
    objective_count: int = Field(default=4, ge=2, le=10)
    range_template: str = Field(default="small-enterprise")
    focus_areas: list[str] = Field(default_factory=list)
    name_override: str | None = Field(None, max_length=255)


class ForgePreviewOut(BaseModel):
    scenario_yaml: str
    model_used: str
    indicators_used: int
    mitre_techniques: list[str]
    estimated_duration_minutes: int
    objective_count: int


class ForgeResultOut(BaseModel):
    exercise_id: uuid.UUID
    scenario_id: uuid.UUID
    name: str
    scenario_yaml: str
    model_used: str
    indicators_used: int
    mitre_techniques: list[str]


class ForgePresetOut(BaseModel):
    name: str
    difficulty: str
    duration_minutes: int
    objective_count: int
    focus_areas: list[str]
    description: str


# ── Adaptive Learning (EPIC 3) ──────────────────────────────────────────


class AutoAssessmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    user_id: uuid.UUID
    exercise_id: uuid.UUID
    competency_mappings: list[dict]
    raw_score: int
    max_score: int
    assessed_at: datetime

    @field_validator("competency_mappings", mode="before")
    @classmethod
    def _parse_competency_mappings(cls, v):
        if isinstance(v, str):
            return json.loads(v)
        return v


class LearningRecommendationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    user_id: uuid.UUID
    summary: str
    strengths: list[str]
    gaps: list[str]
    recommendations: list[dict]
    target_role_readiness: float
    next_milestone: str
    model_used: str
    generated_at: datetime

    @field_validator("strengths", "gaps", "recommendations", mode="before")
    @classmethod
    def _parse_json_fields(cls, v):
        if isinstance(v, str):
            return json.loads(v)
        return v


class ProgressSummaryOut(BaseModel):
    user_id: uuid.UUID
    total_exercises: int
    avg_score: float
    competency_trend: list[dict]
    strongest_areas: list[str]
    weakest_areas: list[str]
    recent_assessments: list[AutoAssessmentOut]


# ── Ops Center (EPIC 2) ─────────────────────────────────────────────────


class AnnotationIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=5000)
    annotation_type: str = Field(default="observation", pattern=r"^(observation|finding|recommendation|ioc)$")
    severity: str = Field(default="info", pattern=r"^(info|low|medium|high|critical)$")
    related_event_id: str | None = None
    tags: list[str] = Field(default_factory=list)


class AnnotationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    exercise_id: uuid.UUID
    user_id: uuid.UUID
    user_display_name: str
    content: str
    annotation_type: str
    severity: str
    related_event_id: str | None = None
    tags: list[str]
    created_at: datetime

    @field_validator("tags", mode="before")
    @classmethod
    def _parse_tags(cls, v):
        if isinstance(v, str):
            return json.loads(v)
        return v


class SharedCommandIn(BaseModel):
    command: str = Field(..., min_length=1, max_length=2000)
    description: str = Field(default="", max_length=500)
    host_tag: str = Field(default="", max_length=100)


class SharedCommandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    exercise_id: uuid.UUID
    user_id: uuid.UUID
    user_display_name: str
    command: str
    description: str
    host_tag: str
    shared_at: datetime


class InstructorInjectIn(BaseModel):
    inject_type: str = Field(..., pattern=r"^(simulated_execution|dns_spike|http_burst|email_phish|custom)$")
    params: dict = Field(default_factory=dict)
    description: str = Field(default="Manual inject from instructor", max_length=1000)


class OpsStatsOut(BaseModel):
    exercise_id: uuid.UUID
    active_analysts: int
    annotations_count: int
    shared_commands_count: int
    objectives_completed: int
    objectives_total: int
    elapsed_seconds: int
