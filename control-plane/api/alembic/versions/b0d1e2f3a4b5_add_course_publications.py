"""add course publications

Revision ID: b0d1e2f3a4b5
Revises: a9c0d1e2f3a4
Create Date: 2026-10-05 15:00:00.000000

One row per accepted release delivered to one Moodle (app/course_publishing): the job's
state, the staging and live course idnumbers, what Moodle reported at each step, the
receipt once live, and the lease that keeps two workers off the same job. Additive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "b0d1e2f3a4b5"
down_revision: str | None = "a9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "course_publications" in sa.inspect(op.get_bind()).get_table_names():
        return  # Base.metadata.create_all got here first (dev boots)
    op.create_table(
        "course_publications",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("release_id", GUID(), sa.ForeignKey("course_releases.id"), nullable=False),
        sa.Column("course_id", GUID(), sa.ForeignKey("courses.id"), nullable=False),
        sa.Column("platform_id", GUID(), sa.ForeignKey("external_platforms.id"), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("stage_idnumber", sa.String(100), nullable=False),
        sa.Column("live_idnumber", sa.String(100), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("remote", sa.Text(), nullable=False),
        sa.Column("receipt", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by", GUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("release_id", "platform_id", name="uq_course_publication"),
    )
    op.create_index("ix_course_publications_state", "course_publications", ["state"])
    op.create_index("ix_course_publications_course", "course_publications", ["course_id", "platform_id"])


def downgrade() -> None:
    op.drop_index("ix_course_publications_course", table_name="course_publications")
    op.drop_index("ix_course_publications_state", table_name="course_publications")
    op.drop_table("course_publications")
