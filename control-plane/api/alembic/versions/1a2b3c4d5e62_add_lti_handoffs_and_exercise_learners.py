"""add lti handoffs and exercise learners

Revision ID: 1a2b3c4d5e62
Revises: 1a2b3c4d5e61
Create Date: 2026-10-09 11:00:00.000000

``lti_handoffs``: single-use, two-minute codes a launched Student exchanges for a TrueNorth
session (hashes only; app/lti_identity/session.py). ``exercise_learners``: who launched an
exercise run from an LMS (attribution, gap #4). Additive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "1a2b3c4d5e62"
down_revision: str | None = "1a2b3c4d5e61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "lti_handoffs" not in tables:
        op.create_table(
            "lti_handoffs",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("bind_hash", sa.String(64), nullable=False),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("platform_id", GUID(), sa.ForeignKey("external_platforms.id"), nullable=False),
            sa.Column("target", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_lti_handoffs_expires", "lti_handoffs", ["expires_at"])
    if "exercise_learners" not in tables:
        op.create_table(
            "exercise_learners",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("exercise_id", GUID(), sa.ForeignKey("exercises.id"), nullable=False),
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("platform_id", GUID(), sa.ForeignKey("external_platforms.id"), nullable=True),
            sa.Column("resource_link_id", sa.String(255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("exercise_id", "user_id", name="uq_exercise_learner"),
        )
        op.create_index("ix_exercise_learners_user", "exercise_learners", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_exercise_learners_user", table_name="exercise_learners")
    op.drop_table("exercise_learners")
    op.drop_index("ix_lti_handoffs_expires", table_name="lti_handoffs")
    op.drop_table("lti_handoffs")
