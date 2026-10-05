"""add course releases

Revision ID: a9c0d1e2f3a4
Revises: f7a8b9c0d1e2
Create Date: 2026-10-05 12:00:00.000000

Immutable accepted versions of a course (app/course_releases): the uploaded ARC² release
tarball stored once by sha256, the release record with its digests and acceptance, and
the pin that keeps an enrollment on the release it started. Additive: no existing table
changes, and a course with no release keeps working as legacy content.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "a9c0d1e2f3a4"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    # Tolerant of a database that Base.metadata.create_all has already touched (dev boots).
    if not _has_table("course_release_blobs"):
        op.create_table(
            "course_release_blobs",
            sa.Column("sha256", sa.String(64), primary_key=True),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("data", sa.LargeBinary(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    if not _has_table("course_releases"):
        op.create_table(
            "course_releases",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("course_id", GUID(), sa.ForeignKey("courses.id"), nullable=False),
            sa.Column("catalogue_code", sa.String(32), nullable=False),
            sa.Column("arc2_code", sa.String(32), nullable=False),
            sa.Column("run_id", sa.String(32), nullable=False),
            sa.Column("slug", sa.String(128), nullable=False),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("release_digest", sa.String(64), nullable=False),
            sa.Column("learner_digest", sa.String(64), nullable=False),
            sa.Column("platform_digest", sa.String(64), nullable=False),
            sa.Column("instructor_digest", sa.String(64), nullable=False),
            sa.Column("blob_sha256", sa.String(64), sa.ForeignKey("course_release_blobs.sha256"), nullable=False),
            sa.Column("meta", sa.Text(), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("created_by", GUID(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("accepted_by", GUID(), nullable=True),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acknowledged_actions", sa.Text(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=False),
            sa.UniqueConstraint("tenant_id", "release_digest", name="uq_course_release_digest"),
            sa.UniqueConstraint("course_id", "version", name="uq_course_release_version"),
        )
        op.create_index("ix_course_releases_course_state", "course_releases", ["course_id", "state"])
    if not _has_table("enrollment_release_pins"):
        op.create_table(
            "enrollment_release_pins",
            sa.Column("enrollment_id", GUID(), sa.ForeignKey("enrollments.id"), primary_key=True),
            sa.Column("release_id", GUID(), sa.ForeignKey("course_releases.id"), nullable=False),
            sa.Column("pinned_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table("enrollment_release_pins")
    op.drop_index("ix_course_releases_course_state", table_name="course_releases")
    op.drop_table("course_releases")
    op.drop_table("course_release_blobs")
