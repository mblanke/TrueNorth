"""TrueNorth Range - SQLAlchemy ORM models.

Designed for 1,200 concurrent users and 70,000 VMs.
All lookup columns carry composite indexes for tenant-scoped queries.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import CHAR, TypeDecorator

from .db import Base


# -- Database-agnostic UUID type ----------------------------------------
class GUID(TypeDecorator):
    """Platform-independent GUID: CHAR(36) on SQLite, native UUID on Postgres."""

    impl = CHAR(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(value)
        return str(value) if isinstance(value, uuid.UUID) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


# -- State-machine transitions (module-level, not inside str enum) ------
_RANGE_TRANSITIONS: dict[str, list[str]] = {
    "created": ["provisioning", "destroyed"],
    "provisioning": ["ready", "failed"],
    "ready": ["running", "destroying"],
    "running": ["stopped", "destroying"],
    "stopped": ["running", "destroying"],
    "destroying": ["destroyed", "failed"],
    "failed": ["provisioning", "destroying", "destroyed"],
}

_EXERCISE_TRANSITIONS: dict[str, list[str]] = {
    "pending": ["running", "cancelled"],
    "running": ["paused", "completed", "cancelled"],
    "paused": ["running", "cancelled"],
    "completed": [],
    "cancelled": [],
}


# -- Enums ---------------------------------------------------------------
class RangeState(str, enum.Enum):
    created = "created"
    provisioning = "provisioning"
    ready = "ready"
    running = "running"
    stopped = "stopped"
    destroying = "destroying"
    destroyed = "destroyed"
    failed = "failed"

    def can_transition_to(self, target: RangeState) -> bool:
        return target.value in _RANGE_TRANSITIONS.get(self.value, [])


class ExerciseState(str, enum.Enum):
    pending = "pending"
    running = "running"
    paused = "paused"
    completed = "completed"
    cancelled = "cancelled"

    def can_transition_to(self, target: ExerciseState) -> bool:
        return target.value in _EXERCISE_TRANSITIONS.get(self.value, [])


class ObjectiveType(str, enum.Enum):
    detection = "detection"
    response = "response"
    deliverable = "deliverable"


class EventState(str, enum.Enum):
    draft = "draft"
    scheduled = "scheduled"
    active = "active"
    completed = "completed"
    cancelled = "cancelled"


class UserRole(str, enum.Enum):
    admin = "admin"
    instructor = "instructor"
    student = "student"
    observer = "observer"
    range_ops = "range_ops"


# -- Mixins ---------------------------------------------------------------
class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SoftDeleteMixin:
    """Mixin providing soft-delete capability with ``deleted_at`` timestamp."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None, index=True
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self) -> None:
        self.deleted_at = datetime.now(tz=__import__("datetime").timezone.utc)


# -- Tenant / User -------------------------------------------------------
class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    users: Mapped[list[User]] = relationship(back_populates="tenant")


class User(SoftDeleteMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_tenant_role", "tenant_id", "role"),
        Index("ix_users_email_lower", "email"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    keycloak_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.student)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # -- Extended identity fields -------------------------------------------
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rank: Mapped[str | None] = mapped_column(String(50), nullable=True)
    service_branch: Mapped[str | None] = mapped_column(String(100), nullable=True)
    nation_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("nations.id"), nullable=True)
    clearance_level: Mapped[str] = mapped_column(String(50), default="unclassified")
    unit: Mapped[str | None] = mapped_column(String(255), nullable=True)
    callsign: Mapped[str | None] = mapped_column(String(50), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # -- AD / directory sync -----------------------------------------------
    source: Mapped[str] = mapped_column(String(20), default="local")
    ad_object_guid: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    ad_distinguished_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- Auth preferences --------------------------------------------------
    auth_method_preference: Mapped[str | None] = mapped_column(String(30), nullable=True)
    timezone: Mapped[str] = mapped_column(String(50), default="UTC")
    tenant: Mapped[Tenant] = relationship(back_populates="users")


class Team(TimestampMixin, Base):
    __tablename__ = "teams"
    __table_args__ = (Index("ix_teams_tenant", "tenant_id"),)
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    team_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    color_hex: Mapped[str | None] = mapped_column(String(7), nullable=True)
    ou_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("organizational_units.id"), nullable=True)
    nation_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("nations.id"), nullable=True)
    max_members: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_persistent: Mapped[bool] = mapped_column(Boolean, default=True)
    exercise_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("exercises.id"), nullable=True)


class TeamMembership(Base):
    __tablename__ = "team_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "team_id", name="uq_user_team"),
        Index("ix_tm_team", "team_id"),
        Index("ix_tm_user", "user_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("teams.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="member")
    position: Mapped[str | None] = mapped_column(String(100), nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# -- Templates ------------------------------------------------------------
class Template(SoftDeleteMixin, TimestampMixin, Base):
    __tablename__ = "templates"
    __table_args__ = (Index("ix_templates_tenant", "tenant_id"),)
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(50), default="0.1.0")
    yaml: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)


# -- Scenarios ------------------------------------------------------------
class Scenario(SoftDeleteMixin, TimestampMixin, Base):
    __tablename__ = "scenarios"
    __table_args__ = (Index("ix_scenarios_tenant", "tenant_id"),)
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(50), default="0.1.0")
    yaml: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)


# -- Ranges ---------------------------------------------------------------
class Range(SoftDeleteMixin, TimestampMixin, Base):
    __tablename__ = "ranges"
    __table_args__ = (
        Index("ix_ranges_tenant_state", "tenant_id", "state"),
        Index("ix_ranges_template", "template_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("templates.id"), nullable=False)
    state: Mapped[RangeState] = mapped_column(Enum(RangeState), default=RangeState.created)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    provisioner_backend: Mapped[str] = mapped_column(String(50), default="mock")
    provisioner_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    template: Mapped[Template] = relationship()
    snapshots: Mapped[list[RangeSnapshot]] = relationship(back_populates="range_", cascade="all, delete-orphan")


class RangeSnapshot(TimestampMixin, Base):
    """A point-in-time snapshot of a range that can be restored."""

    __tablename__ = "range_snapshots"
    __table_args__ = (
        Index("ix_snapshot_range", "range_id"),
        Index("ix_snapshot_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    range_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ranges.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_state: Mapped[str] = mapped_column(
        String(20), default="creating"
    )  # creating, ready, restoring, failed, deleted
    snapshot_data: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: provisioner snapshot refs
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    range_state_at_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    range_: Mapped[Range] = relationship(back_populates="snapshots")


# -- Exercises ------------------------------------------------------------
class Exercise(SoftDeleteMixin, TimestampMixin, Base):
    __tablename__ = "exercises"
    __table_args__ = (
        Index("ix_exercises_tenant_state", "tenant_id", "state"),
        Index("ix_exercises_range", "range_id"),
        Index("ix_exercises_scenario", "scenario_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    range_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ranges.id"), nullable=False)
    scenario_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("scenarios.id"), nullable=False)
    state: Mapped[ExerciseState] = mapped_column(Enum(ExerciseState), default=ExerciseState.pending)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_score: Mapped[int] = mapped_column(Integer, default=0)
    max_score: Mapped[int] = mapped_column(Integer, default=0)
    range_obj: Mapped[Range] = relationship()
    scenario: Mapped[Scenario] = relationship()


class Objective(TimestampMixin, Base):
    __tablename__ = "objectives"
    __table_args__ = (
        Index("ix_objectives_exercise", "exercise_id"),
        Index("ix_objectives_exercise_achieved", "exercise_id", "achieved"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    exercise_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("exercises.id"), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(100), nullable=False)
    objective_type: Mapped[ObjectiveType] = mapped_column(Enum(ObjectiveType), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    validator: Mapped[str] = mapped_column(String(255), nullable=False)
    validator_params: Mapped[str | None] = mapped_column(Text, nullable=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    achieved: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    achieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# -- AAR ------------------------------------------------------------------
class AfterActionReport(TimestampMixin, Base):
    __tablename__ = "after_action_reports"
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    exercise_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("exercises.id"), unique=True, nullable=False)
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    report_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# -- Audit Log ------------------------------------------------------------
class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_tenant_ts", "tenant_id", "timestamp"),
        Index("ix_audit_resource", "resource_type", "resource_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


# -- Scheduled Events (resource reservation) -------------------------------
class ScheduledEvent(TimestampMixin, Base):
    """Resource-reserving event to prevent over-commitment of cluster capacity."""

    __tablename__ = "scheduled_events"
    __table_args__ = (
        Index("ix_event_tenant_start", "tenant_id", "start_time"),
        Index("ix_event_state", "state"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[EventState] = mapped_column(Enum(EventState), default=EventState.draft)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    range_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("ranges.id"), nullable=True)
    template_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("templates.id"), nullable=True)

    # Schedule
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Resource reservation (claimed at schedule time)
    vm_count: Mapped[int] = mapped_column(Integer, default=0)
    vcpu_total: Mapped[int] = mapped_column(Integer, default=0)
    ram_mb_total: Mapped[int] = mapped_column(Integer, default=0)
    disk_gb_total: Mapped[int] = mapped_column(Integer, default=0)

    # Relations
    tenant = relationship("Tenant", lazy="select")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# LMS / Learning Management Models
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


class EnrollmentStatus(str, enum.Enum):
    enrolled = "enrolled"
    in_progress = "in_progress"
    completed = "completed"
    withdrawn = "withdrawn"
    failed = "failed"


class ModuleContentType(str, enum.Enum):
    scenario = "scenario"
    external_lti = "external_lti"
    reading = "reading"
    quiz = "quiz"
    video = "video"


class ModuleProgressStatus(str, enum.Enum):
    not_started = "not_started"
    in_progress = "in_progress"
    completed = "completed"
    skipped = "skipped"


class CompetencyFramework(str, enum.Enum):
    nice = "nice"
    mitre_attack = "mitre_attack"
    custom = "custom"


class ProficiencyLevel(str, enum.Enum):
    novice = "novice"
    beginner = "beginner"
    intermediate = "intermediate"
    advanced = "advanced"
    expert = "expert"


class IntegrationAuthType(str, enum.Enum):
    lti13 = "lti13"
    oauth2 = "oauth2"
    api_key = "api_key"
    saml = "saml"


# -- Course ---------------------------------------------------------------
class Course(TimestampMixin, Base):
    """A structured learning offering composed of ordered modules."""

    __tablename__ = "courses"
    __table_args__ = (
        Index("ix_courses_tenant", "tenant_id"),
        Index("ix_courses_published", "is_published"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[str] = mapped_column(String(50), default="1.0")
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    difficulty: Mapped[str] = mapped_column(String(50), default="intermediate")  # beginner/intermediate/advanced
    duration_hours: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[str] = mapped_column(Text, default="")  # JSON array of tags
    nice_work_roles: Mapped[str] = mapped_column(Text, default="")  # JSON array of NICE work role codes
    course_meta: Mapped[str] = mapped_column(Text, default="{}")  # JSON: prerequisites, learning objectives, etc.

    modules: Mapped[list[CourseModule]] = relationship(back_populates="course", order_by="CourseModule.ordinal")
    enrollments: Mapped[list[Enrollment]] = relationship(back_populates="course")


# -- Course Module --------------------------------------------------------
class CourseModule(TimestampMixin, Base):
    """An ordered unit within a course (scenario, LTI activity, reading, etc.)."""

    __tablename__ = "course_modules"
    __table_args__ = (Index("ix_modules_course_ordinal", "course_id", "ordinal"),)
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    course_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("courses.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    content_type: Mapped[ModuleContentType] = mapped_column(Enum(ModuleContentType), nullable=False)
    # For scenario type: scenario_id; for external_lti: platform_id + resource_link
    content_ref: Mapped[str] = mapped_column(
        Text, default=""
    )  # JSON: {scenario_id, platform_id, resource_link_url, etc.}
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    pass_threshold: Mapped[int] = mapped_column(Integer, default=70)  # percentage needed to pass

    course: Mapped[Course] = relationship(back_populates="modules")


# -- Learning Path --------------------------------------------------------
class LearningPath(TimestampMixin, Base):
    """Ordered collection of courses forming a complete training programme."""

    __tablename__ = "learning_paths"
    __table_args__ = (Index("ix_lp_tenant", "tenant_id"),)
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    course_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON ordered array of course UUIDs
    prerequisite_graph: Mapped[str] = mapped_column(Text, default="{}")  # JSON adjacency list


# -- Enrollment -----------------------------------------------------------
class Enrollment(TimestampMixin, Base):
    """Tracks a user's registration and progress through a course."""

    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("user_id", "course_id", name="uq_user_course"),
        Index("ix_enroll_user", "user_id"),
        Index("ix_enroll_course_status", "course_id", "status"),
        Index("ix_enroll_tenant", "tenant_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    course_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("courses.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    status: Mapped[EnrollmentStatus] = mapped_column(Enum(EnrollmentStatus), default=EnrollmentStatus.enrolled)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    final_grade: Mapped[str | None] = mapped_column(String(10), nullable=True)  # A/B/C/D/F
    final_score: Mapped[int] = mapped_column(Integer, default=0)
    max_score: Mapped[int] = mapped_column(Integer, default=0)

    course: Mapped[Course] = relationship(back_populates="enrollments")
    module_progress: Mapped[list[ModuleProgress]] = relationship(back_populates="enrollment")


# -- Module Progress ------------------------------------------------------
class ModuleProgress(TimestampMixin, Base):
    """Per-user progress through a single course module."""

    __tablename__ = "module_progress"
    __table_args__ = (
        UniqueConstraint("enrollment_id", "module_id", name="uq_enroll_module"),
        Index("ix_mp_enrollment", "enrollment_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    enrollment_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("enrollments.id"), nullable=False)
    module_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("course_modules.id"), nullable=False)
    status: Mapped[ModuleProgressStatus] = mapped_column(
        Enum(ModuleProgressStatus), default=ModuleProgressStatus.not_started
    )
    score: Mapped[int] = mapped_column(Integer, default=0)
    max_score: Mapped[int] = mapped_column(Integer, default=100)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    enrollment: Mapped[Enrollment] = relationship(back_populates="module_progress")


# -- Competency -----------------------------------------------------------
class Competency(TimestampMixin, Base):
    """A skill or knowledge area (e.g. NICE T0023, ATT&CK T1059)."""

    __tablename__ = "competencies"
    __table_args__ = (
        UniqueConstraint("framework", "code", name="uq_framework_code"),
        Index("ix_comp_framework", "framework"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. T0023, T1059.001
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    framework: Mapped[CompetencyFramework] = mapped_column(Enum(CompetencyFramework), nullable=False)
    category: Mapped[str] = mapped_column(String(255), default="")  # e.g. "Analyze", "Protect and Defend"
    level: Mapped[str] = mapped_column(String(50), default="")  # framework-specific level
    parent_code: Mapped[str | None] = mapped_column(String(100), nullable=True)  # hierarchy support


# -- Competency Assertion -------------------------------------------------
class CompetencyAssertion(TimestampMixin, Base):
    """Evidence that a user has demonstrated proficiency in a competency."""

    __tablename__ = "competency_assertions"
    __table_args__ = (
        Index("ix_ca_user", "user_id"),
        Index("ix_ca_competency", "competency_id"),
        Index("ix_ca_user_comp", "user_id", "competency_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    competency_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("competencies.id"), nullable=False)
    proficiency: Mapped[ProficiencyLevel] = mapped_column(Enum(ProficiencyLevel), default=ProficiencyLevel.novice)
    evidence_refs: Mapped[str] = mapped_column(Text, default="[]")  # JSON array: exercise IDs, cert IDs
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source: Mapped[str] = mapped_column(String(100), default="truenorth")  # truenorth/moodle/immersive_labs/offsec


# -- Certification --------------------------------------------------------
class Certification(TimestampMixin, Base):
    """External certification or credential held by a user."""

    __tablename__ = "certifications"
    __table_args__ = (
        Index("ix_certs_user", "user_id"),
        Index("ix_certs_user_issuer", "user_id", "issuer"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    cert_name: Mapped[str] = mapped_column(String(255), nullable=False)  # e.g. OSCP, CompTIA Security+
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)  # e.g. OffSec, CompTIA
    credential_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="active")  # active/expired/revoked
    nice_work_roles: Mapped[str] = mapped_column(Text, default="[]")  # JSON: NICE work roles this cert satisfies
    dod_8140_category: Mapped[str | None] = mapped_column(String(100), nullable=True)  # DCWF work role


# -- External Platform (Moodle, Immersive Labs, OffSec) -------------------
class ExternalPlatform(TimestampMixin, Base):
    """Registration of an external learning platform for integration."""

    __tablename__ = "external_platforms"
    __table_args__ = (
        Index("ix_ep_tenant", "tenant_id"),
        UniqueConstraint("tenant_id", "slug", name="uq_tenant_platform"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)  # e.g. "Company Moodle"
    slug: Mapped[str] = mapped_column(String(100), nullable=False)  # moodle / immersive_labs / offsec / custom
    platform_type: Mapped[str] = mapped_column(String(100), nullable=False)  # moodle / immersive_labs / offsec
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_type: Mapped[IntegrationAuthType] = mapped_column(Enum(IntegrationAuthType), nullable=False)
    credentials_encrypted: Mapped[str] = mapped_column(Text, default="")  # Fernet-encrypted JSON
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # LTI 1.3 specific fields
    lti_client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lti_deployment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lti_issuer: Mapped[str | None] = mapped_column(Text, nullable=True)
    lti_jwks_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    lti_token_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# -- External Activity (cross-platform learning record) -------------------
class ExternalActivity(TimestampMixin, Base):
    """A learning activity completed on an external platform, synced into TrueNorth."""

    __tablename__ = "external_activities"
    __table_args__ = (
        Index("ix_ea_user", "user_id"),
        Index("ix_ea_platform", "platform_id"),
        Index("ix_ea_user_platform", "user_id", "platform_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    platform_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("external_platforms.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    external_ref: Mapped[str] = mapped_column(String(500), nullable=False)  # platform-specific ID
    activity_type: Mapped[str] = mapped_column(String(100), nullable=False)  # course/lab/assessment/certification
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_data: Mapped[str] = mapped_column(Text, default="{}")  # full platform response JSON
    xapi_statement_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # if we generated an xAPI statement


# -- LTI 1.3 Nonce / State (replay protection) ---------------------------
class LTINonce(Base):
    """One-time nonces for LTI 1.3 OIDC login flow â€” replay protection."""

    __tablename__ = "lti_nonces"
    __table_args__ = (Index("ix_lti_nonce_exp", "expires_at"),)
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    nonce: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(255), nullable=False)
    platform_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("external_platforms.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ══════════════════════════════════════════════════════════════════════════
# Infrastructure & Directory Enums
# ══════════════════════════════════════════════════════════════════════════


class HypervisorType(str, enum.Enum):
    proxmox = "proxmox"
    vsphere = "vsphere"
    hyperv = "hyperv"


class HypervisorNodeStatus(str, enum.Enum):
    online = "online"
    offline = "offline"
    maintenance = "maintenance"
    unknown = "unknown"


class AIBackendType(str, enum.Enum):
    ollama = "ollama"
    openai = "openai"
    anthropic = "anthropic"
    azure_openai = "azure_openai"
    mock = "mock"


class AINodeStatus(str, enum.Enum):
    online = "online"
    offline = "offline"
    draining = "draining"
    unknown = "unknown"


class ClearanceLevel(str, enum.Enum):
    unclassified = "unclassified"
    confidential = "confidential"
    secret = "secret"
    top_secret = "top_secret"
    ts_sci = "ts_sci"


class UserSource(str, enum.Enum):
    local = "local"
    ad_sync = "ad_sync"
    federation = "federation"


class TeamType(str, enum.Enum):
    red = "red"
    blue = "blue"
    white = "white"
    purple = "purple"
    green = "green"
    orange = "orange"
    custom = "custom"


class SecurityGroupType(str, enum.Enum):
    access = "access"
    distribution = "distribution"
    training = "training"
    role_based = "role_based"


class OUType(str, enum.Enum):
    country = "country"
    branch = "branch"
    division = "division"
    unit = "unit"
    section = "section"
    custom = "custom"


# ══════════════════════════════════════════════════════════════════════════
# Infrastructure Models
# ══════════════════════════════════════════════════════════════════════════


class HypervisorConnection(TimestampMixin, Base):
    """DB-backed hypervisor endpoint — replaces env-var / hardcoded creds."""

    __tablename__ = "hypervisor_connections"
    __table_args__ = (Index("ix_hv_conn_tenant", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    hypervisor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=8006)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    datacenter: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    nodes: Mapped[list[HypervisorNode]] = relationship(back_populates="connection", cascade="all, delete-orphan")


class HypervisorNode(TimestampMixin, Base):
    """Discovered compute node within a hypervisor cluster."""

    __tablename__ = "hypervisor_nodes"
    __table_args__ = (Index("ix_hv_node_conn", "connection_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("hypervisor_connections.id"), nullable=False)
    node_name: Mapped[str] = mapped_column(String(255), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    cpu_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_used: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_total_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_used_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    storage_total_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    storage_used_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    vm_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connection: Mapped[HypervisorConnection] = relationship(back_populates="nodes")


class HypervisorPool(TimestampMixin, Base):
    """Resource / storage pool within a hypervisor cluster."""

    __tablename__ = "hypervisor_pools"
    __table_args__ = (Index("ix_hv_pool_conn", "connection_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("hypervisor_connections.id"), nullable=False)
    pool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    pool_type: Mapped[str] = mapped_column(String(30), default="cluster")
    total_capacity_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    used_capacity_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


# ══════════════════════════════════════════════════════════════════════════
# AI Orchestrator Config Models
# ══════════════════════════════════════════════════════════════════════════


class AIBackendConfig(TimestampMixin, Base):
    """Persisted AI provider configuration — replaces env-var-only config."""

    __tablename__ = "ai_backend_configs"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    backend_type: Mapped[str] = mapped_column(String(30), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    max_concurrent: Mapped[int] = mapped_column(Integer, default=10)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    fleet_nodes: Mapped[list[AIFleetNode]] = relationship(back_populates="backend", cascade="all, delete-orphan")


class AIFleetNode(TimestampMixin, Base):
    """Individual GPU/inference node within an AI backend fleet."""

    __tablename__ = "ai_fleet_nodes"
    __table_args__ = (Index("ix_ai_fleet_backend", "backend_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    backend_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ai_backend_configs.id"), nullable=False)
    node_name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    gpu_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    gpu_vram_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    loaded_models: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_requests: Mapped[int] = mapped_column(Integer, default=0)
    max_requests: Mapped[int] = mapped_column(Integer, default=10)
    last_health_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    backend: Mapped[AIBackendConfig] = relationship(back_populates="fleet_nodes")


class AIModelRoute(TimestampMixin, Base):
    """Tag-based routing rule: which models go to which backends."""

    __tablename__ = "ai_model_routes"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    model_pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    backend_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ai_backend_configs.id"), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# ══════════════════════════════════════════════════════════════════════════
# Nations & Coalitions
# ══════════════════════════════════════════════════════════════════════════


class Nation(TimestampMixin, Base):
    """Country / nation reference data."""

    __tablename__ = "nations"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    iso_alpha2: Mapped[str] = mapped_column(String(2), unique=True, nullable=False)
    iso_alpha3: Mapped[str] = mapped_column(String(3), unique=True, nullable=False)
    flag_emoji: Mapped[str] = mapped_column(String(10), default="")
    is_nato: Mapped[bool] = mapped_column(Boolean, default=False)
    is_fvey: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Coalition(TimestampMixin, Base):
    """Named alliance or working group of nations."""

    __tablename__ = "coalitions"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class CoalitionMembership(Base):
    """Junction: Nation ↔ Coalition."""

    __tablename__ = "coalition_memberships"
    __table_args__ = (UniqueConstraint("nation_id", "coalition_id", name="uq_nation_coalition"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    nation_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("nations.id"), nullable=False)
    coalition_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("coalitions.id"), nullable=False)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ══════════════════════════════════════════════════════════════════════════
# Organizational Units & Security Groups
# ══════════════════════════════════════════════════════════════════════════


class OrganizationalUnit(TimestampMixin, Base):
    """Hierarchical OU tree — mirrors AD OU structure."""

    __tablename__ = "organizational_units"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    ou_type: Mapped[str] = mapped_column(String(20), default="custom")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("organizational_units.id"), nullable=True)
    dn_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    nation_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("nations.id"), nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    children: Mapped[list[OrganizationalUnit]] = relationship(back_populates="parent")
    parent: Mapped[OrganizationalUnit | None] = relationship(
        back_populates="children", remote_side="OrganizationalUnit.id"
    )


class SecurityGroup(TimestampMixin, Base):
    """Security / distribution group — maps to AD security groups."""

    __tablename__ = "security_groups"
    __table_args__ = (Index("ix_sg_tenant", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    group_type: Mapped[str] = mapped_column(String(20), default="access")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ou_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("organizational_units.id"), nullable=True)
    ad_object_guid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)


class SecurityGroupMembership(Base):
    """Junction: User ↔ SecurityGroup."""

    __tablename__ = "security_group_memberships"
    __table_args__ = (UniqueConstraint("user_id", "group_id", name="uq_user_secgroup"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    group_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("security_groups.id"), nullable=False)


# ══════════════════════════════════════════════════════════════════════════
# Auth Zone Policies
# ══════════════════════════════════════════════════════════════════════════


class AuthZonePolicy(TimestampMixin, Base):
    """Zone-based authentication policy — FIDO2 / Kerberos / session tokens."""

    __tablename__ = "auth_zone_policies"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    zone_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    allowed_methods: Mapped[str] = mapped_column(Text, default="password_mfa,fido2")
    require_mfa: Mapped[bool] = mapped_column(Boolean, default=True)
    session_timeout_minutes: Mapped[int] = mapped_column(Integer, default=480)
    max_failed_attempts: Mapped[int] = mapped_column(Integer, default=5)
    ip_whitelist: Mapped[str | None] = mapped_column(Text, nullable=True)
    clearance_required: Mapped[str] = mapped_column(String(50), default="unclassified")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)


# ══════════════════════════════════════════════════════════════════════════
# Storage
# ══════════════════════════════════════════════════════════════════════════


class StorageProtocol(str, enum.Enum):
    nfs = "nfs"
    iscsi = "iscsi"
    fc = "fc"
    nvme_of = "nvme_of"
    smb = "smb"


class StorageAppliance(TimestampMixin, Base):
    """External storage array (NetApp, Dell, etc.)."""

    __tablename__ = "storage_appliances"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    vendor: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    management_ip: Mapped[str] = mapped_column(String(255), nullable=False)
    protocol: Mapped[StorageProtocol] = mapped_column(default=StorageProtocol.nfs)
    raw_capacity_tb: Mapped[float] = mapped_column(Float, default=0)
    usable_capacity_tb: Mapped[float] = mapped_column(Float, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)


class StorageVolume(TimestampMixin, Base):
    """A volume/LUN exported from a storage appliance."""

    __tablename__ = "storage_volumes"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    appliance_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("storage_appliances.id"), nullable=False)
    volume_name: Mapped[str] = mapped_column(String(255), nullable=False)
    size_gb: Mapped[float] = mapped_column(Float, default=0)
    used_gb: Mapped[float] = mapped_column(Float, default=0)
    protocol: Mapped[StorageProtocol] = mapped_column(default=StorageProtocol.nfs)
    mount_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)


# ══════════════════════════════════════════════════════════════════════════
# Network Devices
# ══════════════════════════════════════════════════════════════════════════


class NetworkDeviceRole(str, enum.Enum):
    tor = "tor"
    spine = "spine"
    leaf = "leaf"
    firewall = "firewall"
    router = "router"
    oob = "oob"


class NetworkDevice(TimestampMixin, Base):
    """Physical network device (switch, firewall, router)."""

    __tablename__ = "network_devices"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    vendor: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[NetworkDeviceRole] = mapped_column(default=NetworkDeviceRole.tor)
    management_ip: Mapped[str] = mapped_column(String(255), nullable=False)
    firmware_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    port_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)


# ══════════════════════════════════════════════════════════════════════════
# Kit Definition
# ══════════════════════════════════════════════════════════════════════════


class KitDefinition(TimestampMixin, Base):
    """A complete deployable kit (rack, nodes, storage, network)."""

    __tablename__ = "kit_definitions"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    compute_node_count: Mapped[int] = mapped_column(Integer, default=0)
    storage_appliance_count: Mapped[int] = mapped_column(Integer, default=0)
    network_device_count: Mapped[int] = mapped_column(Integer, default=0)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)


# ══════════════════════════════════════════════════════════════════════════
# Threat Intelligence
# ══════════════════════════════════════════════════════════════════════════


class ThreatIntelFeed(TimestampMixin, SoftDeleteMixin, Base):
    """A STIX/TAXII or custom threat intelligence feed source."""

    __tablename__ = "threat_intel_feeds"
    __table_args__ = (Index("ix_ti_feed_tenant", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    feed_type: Mapped[str] = mapped_column(String(50), nullable=False)  # taxii, stix_file, custom_api
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    collection_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_key_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    poll_interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_poll_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    indicator_count: Mapped[int] = mapped_column(Integer, default=0)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    indicators: Mapped[list[ThreatIndicator]] = relationship(back_populates="feed", cascade="all, delete-orphan")


class ThreatIndicator(TimestampMixin, Base):
    """An indicator of compromise (IOC) from a threat intelligence feed."""

    __tablename__ = "threat_indicators"
    __table_args__ = (
        Index("ix_ti_indicator_type_value", "indicator_type", "value"),
        Index("ix_ti_indicator_feed", "feed_id"),
        Index("ix_ti_indicator_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    feed_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("threat_intel_feeds.id"), nullable=False)
    stix_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    indicator_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # ipv4, ipv6, domain, url, sha256, md5, email
    value: Mapped[str] = mapped_column(String(2048), nullable=False)
    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[int] = mapped_column(Integer, default=50)  # 0-100
    severity: Mapped[str] = mapped_column(String(20), default="medium")  # low, medium, high, critical
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    kill_chain_phases: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    mitre_attack_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    feed: Mapped[ThreatIntelFeed] = relationship(back_populates="indicators")


# ══════════════════════════════════════════════════════════════════════════
# Detection Rules (Sigma-compatible)
# ══════════════════════════════════════════════════════════════════════════


class DetectionRule(TimestampMixin, SoftDeleteMixin, Base):
    """A Sigma-compatible detection rule for scoring and alerting."""

    __tablename__ = "detection_rules"
    __table_args__ = (
        Index("ix_detection_tenant", "tenant_id"),
        Index("ix_detection_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    sigma_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft, testing, stable, deprecated
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    level: Mapped[str] = mapped_column(String(20), default="medium")  # informational, low, medium, high, critical
    logsource_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    logsource_product: Mapped[str | None] = mapped_column(String(100), nullable=True)
    logsource_service: Mapped[str | None] = mapped_column(String(100), nullable=True)
    detection_yaml: Mapped[str] = mapped_column(Text, nullable=False)  # full Sigma YAML body
    mitre_attack_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    false_positives: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)


# ── Exercise Forge (EPIC 1) ─────────────────────────────────────────────


class ForgedExercise(TimestampMixin, Base):
    """Tracks AI-generated exercises from threat intel feeds."""

    __tablename__ = "forged_exercises"
    __table_args__ = (
        Index("ix_forged_tenant", "tenant_id"),
        Index("ix_forged_feed", "feed_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    exercise_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("exercises.id"), nullable=False)
    scenario_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("scenarios.id"), nullable=False)
    feed_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("threat_intel_feeds.id"), nullable=True)
    indicator_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON array of indicator UUIDs
    scenario_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    mitre_techniques: Mapped[str] = mapped_column(Text, default="[]")  # JSON array
    difficulty: Mapped[str] = mapped_column(String(50), nullable=False)
    model_used: Mapped[str] = mapped_column(String(255), default="")
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)


# ── Adaptive Competency (EPIC 3) ───────────────────────────────────────


class CompetencyAutoAssessment(TimestampMixin, Base):
    """Auto-assessed competencies derived from exercise performance."""

    __tablename__ = "competency_auto_assessments"
    __table_args__ = (
        Index("ix_auto_assess_user", "user_id"),
        Index("ix_auto_assess_exercise", "exercise_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    exercise_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("exercises.id"), nullable=False)
    competency_mappings: Mapped[str] = mapped_column(Text, default="[]")  # JSON: [{competency_id, delta, reason}]
    raw_score: Mapped[int] = mapped_column(Integer, default=0)
    max_score: Mapped[int] = mapped_column(Integer, default=0)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LearningRecommendation(TimestampMixin, Base):
    """AI-generated personalized learning recommendations."""

    __tablename__ = "learning_recommendations"
    __table_args__ = (Index("ix_learn_rec_user", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    strengths: Mapped[str] = mapped_column(Text, default="[]")  # JSON
    gaps: Mapped[str] = mapped_column(Text, default="[]")  # JSON
    recommendations: Mapped[str] = mapped_column(Text, default="[]")  # JSON
    target_role: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_role_readiness: Mapped[float] = mapped_column(Float, default=0.0)
    next_milestone: Mapped[str] = mapped_column(Text, default="")
    model_used: Mapped[str] = mapped_column(String(255), default="")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── Ops Center (EPIC 2) ────────────────────────────────────────────────


class AnalystAnnotation(TimestampMixin, Base):
    """Analyst annotations during live exercise."""

    __tablename__ = "analyst_annotations"
    __table_args__ = (
        Index("ix_annotation_exercise", "exercise_id"),
        Index("ix_annotation_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    exercise_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("exercises.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    user_display_name: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    annotation_type: Mapped[str] = mapped_column(String(50), default="observation")
    severity: Mapped[str] = mapped_column(String(20), default="info")
    related_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tags: Mapped[str] = mapped_column(Text, default="[]")  # JSON array


class SharedCommand(TimestampMixin, Base):
    """Commands shared between analysts during live exercise."""

    __tablename__ = "shared_commands"
    __table_args__ = (Index("ix_shared_cmd_exercise", "exercise_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    exercise_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("exercises.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    user_display_name: Mapped[str] = mapped_column(String(255), default="")
    command: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    host_tag: Mapped[str] = mapped_column(String(100), default="")
    shared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
