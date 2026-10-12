"""add enrollments.source: a Student's own self-enrolment vs an assigned one

Revision ID: a1e2b3c4d5f6
Revises: 9e5c3a7b2d18
Create Date: 2026-10-12 15:00:00.000000

A Student who enrols themselves (POST /courses/{id}/enroll on their own account) gets the
course's content but no seat in an exercise someone booked for that course: rosters
(app/exercise_participants.py, app/telemetry_access.py) count only ``assigned`` enrolments
(admin, approval, path, LTI/Moodle sync, import). Every row that exists before this
revision was made by one of those paths, so it is ``assigned``. Additive; downgrade drops it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1e2b3c4d5f6"
down_revision: str | None = "9e5c3a7b2d18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("enrollments")}


def upgrade() -> None:
    if "source" in _columns():
        return
    with op.batch_alter_table("enrollments") as batch:
        batch.add_column(sa.Column("source", sa.String(16), nullable=False, server_default="assigned"))


def downgrade() -> None:
    if "source" in _columns():
        with op.batch_alter_table("enrollments") as batch:
            batch.drop_column("source")
