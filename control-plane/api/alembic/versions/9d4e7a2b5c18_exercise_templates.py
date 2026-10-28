"""exercise templates: the reusable exercise, and which exercises came from which

Revision ID: 9d4e7a2b5c18
Revises: 7c4d2e9a1b60
Create Date: 2026-10-28 09:00:00.000000

ADR 0017 (``app/exercise_templates``): an exercise template holds what is reusable about an
exercise (objectives, the MESL, the scenario and range template, the teams, the participant
roles with their VM profiles and the shared-infrastructure scaling rules, the instructor's
notes), proven and portable through the content port.

* ``exercise_templates``: the template; ``revision`` rises on every edit; ``tenant_id`` and
  ``is_public`` scope it as scenarios and range templates are scoped.
* ``exercise_template_uses``: one row per exercise made from a template, with the revision it
  came from. ``exercises`` itself is not changed: an exercise made by hand has no row here.

Additive; no rows are written. Downgrade drops both tables; the exercises stay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "9d4e7a2b5c18"
down_revision: str | None = "7c4d2e9a1b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "exercise_templates" not in existing:
        op.create_table(
            "exercise_templates",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("is_public", sa.Boolean(), nullable=False),
            sa.Column("scenario_id", GUID(), sa.ForeignKey("scenarios.id"), nullable=True),
            sa.Column("range_template_id", GUID(), sa.ForeignKey("templates.id"), nullable=True),
            sa.Column("teams", sa.JSON(), nullable=False),
            sa.Column("roles", sa.JSON(), nullable=False),
            sa.Column("scaling_rules", sa.JSON(), nullable=False),
            sa.Column("objectives", sa.JSON(), nullable=False),
            sa.Column("mesl", sa.JSON(), nullable=False),
            sa.Column("instructor_notes", sa.Text(), nullable=False),
            sa.Column("tags", sa.JSON(), nullable=False),
            sa.Column("created_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_exercise_templates_tenant", "exercise_templates", ["tenant_id"])
        op.create_index("ix_exercise_templates_deleted_at", "exercise_templates", ["deleted_at"])
    if "exercise_template_uses" not in existing:
        op.create_table(
            "exercise_template_uses",
            sa.Column("exercise_id", GUID(), sa.ForeignKey("exercises.id"), primary_key=True),
            sa.Column("template_id", GUID(), sa.ForeignKey("exercise_templates.id"), nullable=False),
            sa.Column("template_revision", sa.Integer(), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_exercise_template_uses_template", "exercise_template_uses", ["template_id"])


def downgrade() -> None:
    op.drop_index("ix_exercise_template_uses_template", table_name="exercise_template_uses")
    op.drop_table("exercise_template_uses")
    op.drop_index("ix_exercise_templates_deleted_at", table_name="exercise_templates")
    op.drop_index("ix_exercise_templates_tenant", table_name="exercise_templates")
    op.drop_table("exercise_templates")
