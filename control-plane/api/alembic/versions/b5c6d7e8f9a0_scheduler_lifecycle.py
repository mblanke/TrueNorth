"""scheduler lifecycle: `provisioning` state, instructor and creator (ADR 0004 slice 3)

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-10-06 10:00:00.000000

b0c1d2e3f4a5 builds `scheduled_events` from the live ORM on an empty database, so
there the columns and the enum value already exist. Each step is skipped when it is
already in place.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

# revision identifiers, used by Alembic.
revision: str = "b5c6d7e8f9a0"
down_revision: str | None = "a4b5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("instructor_id", "created_by")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # SQLite stores the enum as VARCHAR; Postgres needs the new label.
        op.execute("ALTER TYPE eventstate ADD VALUE IF NOT EXISTS 'provisioning' AFTER 'scheduled'")
    have = {c["name"] for c in sa.inspect(bind).get_columns("scheduled_events")}
    missing = [name for name in COLUMNS if name not in have]
    if not missing:
        return
    # Batch mode: SQLite cannot add a column with a foreign key in place.
    with op.batch_alter_table("scheduled_events") as batch:
        for name in missing:
            batch.add_column(
                sa.Column(name, GUID(), sa.ForeignKey("users.id", name=f"scheduled_events_{name}_fkey"), nullable=True)
            )


def downgrade() -> None:
    # Postgres cannot drop an enum label; `provisioning` stays.
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("scheduled_events")}
    gone = [name for name in COLUMNS if name in have]
    if not gone:
        return
    # SQLite cannot drop a column with a foreign key in place; batch mode rebuilds the table.
    with op.batch_alter_table("scheduled_events") as batch:
        for name in gone:
            batch.drop_column(name)
