"""add exercise runs

Revision ID: e4f5a6b7c8d9
Revises: f4b5c6d7e8a9
Create Date: 2026-10-08 12:00:00.000000

The current run of each exercise's timeline (run id, the lease of the one worker task
allowed to fire it, and its claim cursor), so a paused exercise can be resumed from the
next unfired event (app/scenario_runs/models.py ``ExerciseRun``). Additive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "e4f5a6b7c8d9"
down_revision: str | None = "f4b5c6d7e8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "exercise_runs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "exercise_runs",
        sa.Column("exercise_id", GUID(), sa.ForeignKey("exercises.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("lease", sa.String(64), nullable=False),
        sa.Column("next_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resume_from", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("exercise_runs")
