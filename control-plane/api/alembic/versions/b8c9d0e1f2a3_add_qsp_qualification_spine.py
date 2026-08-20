"""add CFITES/QSP qualification spine (qualifications, POs, EOs, lessons, module content, range map)

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-17 19:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from app.models import GUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enum DB labels match SQLAlchemy's default (Python enum *member names*).
_PO_TIER = sa.Enum("core", "gate", name="potier")
_PO_STATUS = sa.Enum("todo", "example", "needs_spec", "offensive_author", "cots_gate", "done", name="postatus")
_QSP_ENV = sa.Enum("cste", "cste_sterile", "cote", "mobile", name="qspenvironment")
_CONTENT_KIND = sa.Enum("teach", "check", "assess", name="contentkind")


def upgrade() -> None:
    op.create_table(
        "qualifications",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("qsp_code", sa.String(32), nullable=False, unique=True),
        sa.Column("nqual", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("mite_course_code", sa.String(32), nullable=False, server_default=""),
        sa.Column("component_version", sa.String(20), nullable=False, server_default="v2.1.0"),
        sa.Column("target_role", sa.String(120), nullable=False, server_default=""),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_qual_tenant", "qualifications", ["tenant_id"])

    op.create_table(
        "performance_objectives",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("qualification_id", GUID(), sa.ForeignKey("qualifications.id"), nullable=False),
        sa.Column("po_code", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("tier", _PO_TIER, nullable=False, server_default="core"),
        sa.Column("conditions", sa.Text(), nullable=False, server_default=""),
        sa.Column("critical_events", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("assessment_type", sa.String(120), nullable=False, server_default=""),
        sa.Column("duration_min", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pass_standard", sa.String(120), nullable=False, server_default=""),
        sa.Column("deliverable", sa.String(255), nullable=False, server_default=""),
        sa.Column("environment", _QSP_ENV, nullable=False, server_default="cote"),
        sa.Column("target_role", sa.String(120), nullable=False, server_default=""),
        sa.Column("nice_dcwf_task", sa.String(120), nullable=False, server_default=""),
        sa.Column("scenario_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("build_hours", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", _PO_STATUS, nullable=False, server_default="todo"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("qualification_id", "po_code", name="uq_qual_po"),
    )
    op.create_index("ix_po_qual", "performance_objectives", ["qualification_id"])
    op.create_index("ix_po_status", "performance_objectives", ["status"])

    op.create_table(
        "enabling_objectives",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("po_id", GUID(), sa.ForeignKey("performance_objectives.id"), nullable=False),
        sa.Column("eo_code", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("maps_to_critical_event", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("po_id", "eo_code", name="uq_po_eo"),
    )
    op.create_index("ix_eo_po", "enabling_objectives", ["po_id"])

    op.create_table(
        "lessons",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False, server_default=""),
        sa.Column("media_refs", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("competency_codes", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("source_curriculum_id", GUID(), sa.ForeignKey("curricula.id"), nullable=True),
        sa.Column("generated_by_model", sa.String(120), nullable=False, server_default=""),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_lessons_tenant", "lessons", ["tenant_id"])

    op.create_table(
        "lesson_objectives",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("lesson_id", GUID(), sa.ForeignKey("lessons.id"), nullable=False),
        sa.Column("eo_id", GUID(), sa.ForeignKey("enabling_objectives.id"), nullable=False),
        sa.UniqueConstraint("lesson_id", "eo_id", name="uq_lesson_eo"),
    )

    op.create_table(
        "module_content",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("module_id", GUID(), sa.ForeignKey("course_modules.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_kind", _CONTENT_KIND, nullable=False, server_default="teach"),
        sa.Column("lesson_id", GUID(), sa.ForeignKey("lessons.id"), nullable=True),
        sa.Column("quiz_id", GUID(), sa.ForeignKey("quizzes.id"), nullable=True),
        sa.Column("scenario_id", GUID(), sa.ForeignKey("scenarios.id"), nullable=True),
        sa.Column("external_ref", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_modcontent_module_ordinal", "module_content", ["module_id", "ordinal"])

    op.create_table(
        "range_objective_map",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("template_id", GUID(), sa.ForeignKey("templates.id"), nullable=False),
        sa.Column("po_id", GUID(), sa.ForeignKey("performance_objectives.id"), nullable=False),
        sa.Column("source", sa.String(40), nullable=False, server_default="manual"),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("template_id", "po_id", name="uq_template_po"),
    )
    op.create_index("ix_rom_template", "range_objective_map", ["template_id"])
    op.create_index("ix_rom_po", "range_objective_map", ["po_id"])

    # Anchor existing LMS tables to the QSP spine (batch mode → SQLite-safe FK add)
    with op.batch_alter_table("courses") as batch:
        batch.add_column(sa.Column("qualification_id", GUID(), sa.ForeignKey("qualifications.id"), nullable=True))
    with op.batch_alter_table("course_modules") as batch:
        batch.add_column(sa.Column("po_id", GUID(), sa.ForeignKey("performance_objectives.id"), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("course_modules") as batch:
        batch.drop_column("po_id")
    with op.batch_alter_table("courses") as batch:
        batch.drop_column("qualification_id")

    op.drop_index("ix_rom_po", table_name="range_objective_map")
    op.drop_index("ix_rom_template", table_name="range_objective_map")
    op.drop_table("range_objective_map")
    op.drop_index("ix_modcontent_module_ordinal", table_name="module_content")
    op.drop_table("module_content")
    op.drop_table("lesson_objectives")
    op.drop_index("ix_lessons_tenant", table_name="lessons")
    op.drop_table("lessons")
    op.drop_index("ix_eo_po", table_name="enabling_objectives")
    op.drop_table("enabling_objectives")
    op.drop_index("ix_po_status", table_name="performance_objectives")
    op.drop_index("ix_po_qual", table_name="performance_objectives")
    op.drop_table("performance_objectives")
    op.drop_index("ix_qual_tenant", table_name="qualifications")
    op.drop_table("qualifications")

    for enum_type in (_CONTENT_KIND, _QSP_ENV, _PO_STATUS, _PO_TIER):
        enum_type.drop(op.get_bind(), checkfirst=True)
