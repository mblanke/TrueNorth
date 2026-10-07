"""add detection submissions

Revision ID: e3a4b5c6d7e8
Revises: d2f3a4b5c6d7
Create Date: 2026-10-06 19:00:00.000000

Every detection a Student submits against an exercise objective, with the server's
verdict (ADR 0005, app/detections). Additive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "e3a4b5c6d7e8"
down_revision: str | None = "d2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "detection_submissions" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "detection_submissions",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("exercise_id", GUID(), sa.ForeignKey("exercises.id"), nullable=False),
        sa.Column("objective_ref", sa.String(100), nullable=False),
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("events_matched", sa.Integer(), nullable=True),
        sa.Column("on_target", sa.Integer(), nullable=True),
        sa.Column("threshold", sa.Integer(), nullable=True),
        sa.Column("min_precision", sa.Float(), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("matched_ids", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_detection_submissions_attempts", "detection_submissions", ["exercise_id", "objective_ref", "user_id"]
    )
    op.create_index("ix_detection_submissions_tenant", "detection_submissions", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_detection_submissions_tenant", table_name="detection_submissions")
    op.drop_index("ix_detection_submissions_attempts", table_name="detection_submissions")
    op.drop_table("detection_submissions")
