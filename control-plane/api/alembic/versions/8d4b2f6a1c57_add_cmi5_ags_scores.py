"""add cmi5_ags_scores: AU results going back to an LMS gradebook over LTI AGS

Revision ID: 8d4b2f6a1c57
Revises: 7c3a1e5f9d42
Create Date: 2026-10-10 12:00:00.000000

A cmi5 AU launched from Moodle over LTI 1.3 reports its result to Moodle's gradebook
(app/cmi5/ags.py): one row per gradebook cell (platform, line item, the platform's user),
written with the result and sent after the transaction commits, re-sent on transient
failure. Additive; downgrade drops it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "8d4b2f6a1c57"
down_revision: str | None = "7c3a1e5f9d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "cmi5_ags_scores" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "cmi5_ags_scores",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("cell_key", sa.String(64), nullable=False, unique=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("platform_id", GUID(), sa.ForeignKey("external_platforms.id"), nullable=False),
        sa.Column("registration_id", GUID(), sa.ForeignKey("cmi5_registrations.id"), nullable=False),
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("release_id", GUID(), sa.ForeignKey("course_releases.id"), nullable=False),
        sa.Column("au_index", sa.Integer(), nullable=False),
        sa.Column("lineitem_url", sa.Text(), nullable=False),
        sa.Column("lti_user_sub", sa.String(255), nullable=False),
        sa.Column("score_given", sa.Float(), nullable=True),
        sa.Column("activity_progress", sa.String(16), nullable=False),
        sa.Column("grading_progress", sa.String(16), nullable=False),
        sa.Column("result_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(8), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_cmi5_ags_due", "cmi5_ags_scores", ["state", "next_attempt_at"])
    op.create_index("ix_cmi5_ags_reg", "cmi5_ags_scores", ["registration_id"])


def downgrade() -> None:
    if "cmi5_ags_scores" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_cmi5_ags_reg", table_name="cmi5_ags_scores")
        op.drop_index("ix_cmi5_ags_due", table_name="cmi5_ags_scores")
        op.drop_table("cmi5_ags_scores")
