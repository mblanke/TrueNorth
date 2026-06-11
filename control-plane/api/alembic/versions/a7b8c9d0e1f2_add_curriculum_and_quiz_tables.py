"""add curriculum forge and quiz engine tables

Revision ID: a7b8c9d0e1f2
Revises: f6a1b2c3d4e5
Create Date: 2026-06-11 16:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.models import GUID

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "curricula",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.Enum("draft", "ingesting", "ready", "error", name="curriculumstatus"),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding_model", sa.String(100), nullable=False, server_default=""),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_curricula_tenant", "curricula", ["tenant_id"])

    op.create_table(
        "curriculum_documents",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("curriculum_id", GUID(), sa.ForeignKey("curricula.id"), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("mime_type", sa.String(120), nullable=False, server_default=""),
        sa.Column("minio_key", sa.String(600), nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.Enum("pending", "extracting", "embedding", "indexed", "error", name="curriculumdocstatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_curr_docs_curriculum", "curriculum_documents", ["curriculum_id"])

    op.create_table(
        "quizzes",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("module_id", GUID(), sa.ForeignKey("course_modules.id"), nullable=True),
        sa.Column("curriculum_id", GUID(), sa.ForeignKey("curricula.id"), nullable=True),
        sa.Column("pass_pct", sa.Integer(), nullable=False, server_default="70"),
        sa.Column("time_limit_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shuffle_questions", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("generated_by_model", sa.String(120), nullable=False, server_default=""),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_quizzes_tenant", "quizzes", ["tenant_id"])

    op.create_table(
        "quiz_questions",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("quiz_id", GUID(), sa.ForeignKey("quizzes.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "question_type",
            sa.Enum("mcq", "multi", "truefalse", "scenario", name="quizquestiontype"),
            nullable=False,
            server_default="mcq",
        ),
        sa.Column("stem", sa.Text(), nullable=False),
        sa.Column("options", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("correct", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("explanation", sa.Text(), nullable=False, server_default=""),
        sa.Column("competency_code", sa.String(50), nullable=False, server_default=""),
        sa.Column("difficulty", sa.String(20), nullable=False, server_default="intermediate"),
        sa.Column("points", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_quiz_questions_quiz", "quiz_questions", ["quiz_id", "ordinal"])

    op.create_table(
        "quiz_attempts",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("quiz_id", GUID(), sa.ForeignKey("quizzes.id"), nullable=False),
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answers", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("question_order", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_quiz_attempts_user", "quiz_attempts", ["user_id", "quiz_id"])

    # Phase 4: objectives carry an optional competency mapping
    op.add_column("objectives", sa.Column("competency_code", sa.String(50), nullable=True))

    # Phase 5: auto-assessments can originate from quiz attempts, not just exercises
    with op.batch_alter_table("competency_auto_assessments") as batch:
        batch.alter_column("exercise_id", existing_type=GUID(), nullable=True)
        batch.add_column(
            sa.Column("quiz_attempt_id", GUID(), sa.ForeignKey("quiz_attempts.id"), nullable=True)
        )

    # Phase 6: LTI 1.3 tool keys + launch records + platform OIDC auth URL
    op.create_table(
        "lti_tool_keys",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("kid", sa.String(100), nullable=False, unique=True),
        sa.Column("private_key_pem", sa.Text(), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "lti_launches",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("platform_id", GUID(), sa.ForeignKey("external_platforms.id"), nullable=False),
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("lti_user_sub", sa.String(255), nullable=False),
        sa.Column("resource_kind", sa.String(50), nullable=False, server_default=""),
        sa.Column("resource_id", sa.String(100), nullable=False, server_default=""),
        sa.Column("resource_link_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("context_title", sa.String(500), nullable=False, server_default=""),
        sa.Column("ags_lineitem_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("ags_scopes", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_lti_launch_user_resource", "lti_launches", ["user_id", "resource_kind", "resource_id"]
    )
    op.add_column("external_platforms", sa.Column("lti_auth_login_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("external_platforms", "lti_auth_login_url")
    op.drop_index("ix_lti_launch_user_resource", table_name="lti_launches")
    op.drop_table("lti_launches")
    op.drop_table("lti_tool_keys")
    with op.batch_alter_table("competency_auto_assessments") as batch:
        batch.drop_column("quiz_attempt_id")
        batch.alter_column("exercise_id", existing_type=GUID(), nullable=False)
    op.drop_column("objectives", "competency_code")
    op.drop_index("ix_quiz_attempts_user", table_name="quiz_attempts")
    op.drop_table("quiz_attempts")
    op.drop_index("ix_quiz_questions_quiz", table_name="quiz_questions")
    op.drop_table("quiz_questions")
    op.drop_index("ix_quizzes_tenant", table_name="quizzes")
    op.drop_table("quizzes")
    op.drop_index("ix_curr_docs_curriculum", table_name="curriculum_documents")
    op.drop_table("curriculum_documents")
    op.drop_index("ix_curricula_tenant", table_name="curricula")
    op.drop_table("curricula")
