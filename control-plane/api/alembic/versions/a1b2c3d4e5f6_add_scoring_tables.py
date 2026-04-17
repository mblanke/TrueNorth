"""add scoring tables

Revision ID: a1b2c3d4e5f6
Revises: 3731bf01ced3
Create Date: 2026-02-26 06:00:00.000000
"""

import os
import sys
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from app.models import GUID

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "3731bf01ced3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scoring_results",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("exercise_id", GUID(), sa.ForeignKey("exercises.id"), nullable=False),
        sa.Column("team_id", GUID(), sa.ForeignKey("teams.id"), nullable=True),
        sa.Column("total_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("percentage", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("grade", sa.String(length=2), nullable=True),
        sa.Column("time_elapsed_seconds", sa.Integer(), nullable=True),
        sa.Column("bonuses_json", sa.Text(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scoring_results_exercise", "scoring_results", ["exercise_id"])
    op.create_index("ix_scoring_results_team", "scoring_results", ["team_id"])

    op.create_table(
        "objective_results",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("scoring_result_id", GUID(), sa.ForeignKey("scoring_results.id", ondelete="CASCADE"), nullable=False),
        sa.Column("objective_id", GUID(), nullable=False),
        sa.Column("achieved", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("points_awarded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("achieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_obj_results_scoring", "objective_results", ["scoring_result_id"])
    op.create_index("ix_obj_results_objective", "objective_results", ["objective_id"])

    op.create_table(
        "leaderboard_entries",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("exercise_id", GUID(), sa.ForeignKey("exercises.id"), nullable=False),
        sa.Column("team_id", GUID(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exercise_id", "team_id", name="uq_leaderboard_exercise_team"),
    )
    op.create_index("ix_leaderboard_exercise_score", "leaderboard_entries", ["exercise_id", "score"])


def downgrade() -> None:
    op.drop_index("ix_leaderboard_exercise_score", table_name="leaderboard_entries")
    op.drop_table("leaderboard_entries")
    op.drop_index("ix_obj_results_objective", table_name="objective_results")
    op.drop_index("ix_obj_results_scoring", table_name="objective_results")
    op.drop_table("objective_results")
    op.drop_index("ix_scoring_results_team", table_name="scoring_results")
    op.drop_index("ix_scoring_results_exercise", table_name="scoring_results")
    op.drop_table("scoring_results")
