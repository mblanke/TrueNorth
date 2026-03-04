"""initial_schema

Revision ID: 3731bf01ced3
Revises:
Create Date: 2026-02-25 16:20:40.833652
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Import custom GUID type
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from app.models import GUID


# revision identifiers
revision: str = "3731bf01ced3"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- tenants ---
    op.create_table(
        "tenants",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(op.f("ix_tenants_slug"), "tenants", ["slug"], unique=True)

    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("keycloak_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.Enum("admin", "instructor", "student", "observer", "range_ops", name="userrole"), nullable=False),
        sa.Column("tenant_id", GUID(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("keycloak_id"),
    )
    op.create_index("ix_users_email_lower", "users", ["email"], unique=False)
    op.create_index("ix_users_tenant_role", "users", ["tenant_id", "role"], unique=False)

    # --- teams ---
    op.create_table(
        "teams",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", GUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_teams_tenant", "teams", ["tenant_id"], unique=False)

    # --- team_memberships ---
    op.create_table(
        "team_memberships",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("user_id", GUID(), nullable=False),
        sa.Column("team_id", GUID(), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "team_id", name="uq_user_team"),
    )
    op.create_index("ix_tm_team", "team_memberships", ["team_id"], unique=False)
    op.create_index("ix_tm_user", "team_memberships", ["user_id"], unique=False)

    # --- templates ---
    op.create_table(
        "templates",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("yaml", sa.Text(), nullable=False),
        sa.Column("tenant_id", GUID(), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_templates_tenant", "templates", ["tenant_id"], unique=False)

    # --- scenarios ---
    op.create_table(
        "scenarios",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("yaml", sa.Text(), nullable=False),
        sa.Column("tenant_id", GUID(), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scenarios_tenant", "scenarios", ["tenant_id"], unique=False)

    # --- ranges ---
    op.create_table(
        "ranges",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("template_id", GUID(), nullable=False),
        sa.Column("state", sa.Enum("created", "provisioning", "ready", "running", "stopped", "destroying", "destroyed", "failed", name="rangestate"), nullable=False),
        sa.Column("tenant_id", GUID(), nullable=True),
        sa.Column("provisioner_backend", sa.String(length=50), nullable=False),
        sa.Column("provisioner_output", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["template_id"], ["templates.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ranges_template", "ranges", ["template_id"], unique=False)
    op.create_index("ix_ranges_tenant_state", "ranges", ["tenant_id", "state"], unique=False)

    # --- exercises ---
    op.create_table(
        "exercises",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("range_id", GUID(), nullable=False),
        sa.Column("scenario_id", GUID(), nullable=False),
        sa.Column("state", sa.Enum("pending", "running", "paused", "completed", "cancelled", name="exercisestate"), nullable=False),
        sa.Column("tenant_id", GUID(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_score", sa.Integer(), nullable=False),
        sa.Column("max_score", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["range_id"], ["ranges.id"]),
        sa.ForeignKeyConstraint(["scenario_id"], ["scenarios.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_exercises_range", "exercises", ["range_id"], unique=False)
    op.create_index("ix_exercises_scenario", "exercises", ["scenario_id"], unique=False)
    op.create_index("ix_exercises_tenant_state", "exercises", ["tenant_id", "state"], unique=False)

    # --- objectives ---
    op.create_table(
        "objectives",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("exercise_id", GUID(), nullable=False),
        sa.Column("ref_id", sa.String(length=100), nullable=False),
        sa.Column("objective_type", sa.Enum("detection", "response", "deliverable", name="objectivetype"), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("validator", sa.String(length=255), nullable=False),
        sa.Column("validator_params", sa.Text(), nullable=True),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("achieved", sa.Boolean(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("achieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_objectives_exercise", "objectives", ["exercise_id"], unique=False)
    op.create_index("ix_objectives_exercise_achieved", "objectives", ["exercise_id", "achieved"], unique=False)

    # --- after_action_reports ---
    op.create_table(
        "after_action_reports",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("exercise_id", GUID(), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("report_html", sa.Text(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exercise_id"),
    )

    # --- audit_logs ---
    op.create_table(
        "audit_logs",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("user_id", GUID(), nullable=True),
        sa.Column("tenant_id", GUID(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_resource", "audit_logs", ["resource_type", "resource_id"], unique=False)
    op.create_index("ix_audit_tenant_ts", "audit_logs", ["tenant_id", "timestamp"], unique=False)


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("after_action_reports")
    op.drop_index("ix_objectives_exercise_achieved", table_name="objectives")
    op.drop_index("ix_objectives_exercise", table_name="objectives")
    op.drop_table("objectives")
    op.drop_index("ix_exercises_tenant_state", table_name="exercises")
    op.drop_index("ix_exercises_scenario", table_name="exercises")
    op.drop_index("ix_exercises_range", table_name="exercises")
    op.drop_table("exercises")
    op.drop_index("ix_ranges_tenant_state", table_name="ranges")
    op.drop_index("ix_ranges_template", table_name="ranges")
    op.drop_table("ranges")
    op.drop_index("ix_scenarios_tenant", table_name="scenarios")
    op.drop_table("scenarios")
    op.drop_index("ix_templates_tenant", table_name="templates")
    op.drop_table("templates")
    op.drop_index("ix_tm_user", table_name="team_memberships")
    op.drop_index("ix_tm_team", table_name="team_memberships")
    op.drop_table("team_memberships")
    op.drop_index("ix_teams_tenant", table_name="teams")
    op.drop_table("teams")
    op.drop_index("ix_users_tenant_role", table_name="users")
    op.drop_index("ix_users_email_lower", table_name="users")
    op.drop_table("users")
    op.drop_index(op.f("ix_tenants_slug"), table_name="tenants")
    op.drop_table("tenants")

    # Drop enums (PostgreSQL only)
    sa.Enum(name="objectivetype").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="exercisestate").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="rangestate").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="userrole").drop(op.get_bind(), checkfirst=True)