"""scheduler: a booking's class is a course (ADR 0004 slice 10)

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-10-06 18:30:00.000000

Skipped when present: b0c1d2e3f4a5 builds `scheduled_events` from the live ORM on an
empty database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

# revision identifiers, used by Alembic.
revision: str = "e8f9a0b1c2d3"
down_revision: str | None = "d7e8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_course_id() -> bool:
    return "course_id" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("scheduled_events")}


def upgrade() -> None:
    if not _has_course_id():
        # Batch mode: SQLite cannot add a column with a foreign key in place.
        with op.batch_alter_table("scheduled_events") as batch:
            batch.add_column(
                sa.Column(
                    "course_id",
                    GUID(),
                    sa.ForeignKey("courses.id", name="scheduled_events_course_id_fkey"),
                    nullable=True,
                )
            )


def downgrade() -> None:
    if _has_course_id():
        # SQLite cannot drop a column with a foreign key in place; batch mode rebuilds the table.
        with op.batch_alter_table("scheduled_events") as batch:
            batch.drop_column("course_id")
