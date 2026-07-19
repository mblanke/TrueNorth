"""add collective exercise kind + objectives + MESL events

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-19 15:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.models import GUID

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Exercise gains a kind discriminator; scenario_id becomes optional (collective = MESL-driven).
    with op.batch_alter_table("exercises") as batch:
        batch.add_column(sa.Column("kind", sa.String(20), nullable=False, server_default="assessment"))
        batch.alter_column("scenario_id", existing_type=GUID(), nullable=True)

    op.create_table(
        "exercise_objectives",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("exercise_id", GUID(), sa.ForeignKey("exercises.id"), nullable=False),
        sa.Column("ref", sa.String(32), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("moe", sa.Text(), nullable=False, server_default=""),
        sa.Column("competency_code", sa.String(50), nullable=False, server_default=""),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_exobj_exercise", "exercise_objectives", ["exercise_id"])

    op.create_table(
        "mesl_events",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("exercise_id", GUID(), sa.ForeignKey("exercises.id"), nullable=False),
        sa.Column("serial", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("phase", sa.String(40), nullable=False, server_default=""),
        sa.Column("scenario_time", sa.String(40), nullable=False, server_default=""),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("objective_ref", sa.String(32), nullable=False, server_default=""),
        sa.Column("attack_technique", sa.String(32), nullable=False, server_default=""),
        sa.Column("delivery_method", sa.String(20), nullable=False, server_default="cyber"),
        sa.Column("from_cell", sa.String(80), nullable=False, server_default=""),
        sa.Column("to_participant", sa.String(80), nullable=False, server_default=""),
        sa.Column("expected_action", sa.Text(), nullable=False, server_default=""),
        sa.Column("moe", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="planned"),
        sa.Column("generated_by_model", sa.String(120), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_mesl_exercise_serial", "mesl_events", ["exercise_id", "serial"])


def downgrade() -> None:
    op.drop_index("ix_mesl_exercise_serial", table_name="mesl_events")
    op.drop_table("mesl_events")
    op.drop_index("ix_exobj_exercise", table_name="exercise_objectives")
    op.drop_table("exercise_objectives")
    with op.batch_alter_table("exercises") as batch:
        batch.alter_column("scenario_id", existing_type=GUID(), nullable=False)
        batch.drop_column("kind")
