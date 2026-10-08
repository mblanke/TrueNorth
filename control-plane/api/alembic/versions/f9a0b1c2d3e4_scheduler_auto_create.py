"""scheduler: a booking's scenario and exercise (ADR 0004 slice 11)

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-10-06 20:00:00.000000

Skipped when present: b0c1d2e3f4a5 builds `scheduled_events` from the live ORM on an
empty database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

# revision identifiers, used by Alembic.
revision: str = "f9a0b1c2d3e4"
down_revision: str | None = "e8f9a0b1c2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def _keyed(name: str, target: str):
    # Named as Postgres names an added column's key, so batch mode (SQLite) can find it.
    return lambda: sa.Column(name, GUID(), sa.ForeignKey(target, name=f"scheduled_events_{name}_fkey"), nullable=True)


COLUMNS = (
    ("scenario_id", _keyed("scenario_id", "scenarios.id")),
    ("exercise_id", _keyed("exercise_id", "exercises.id")),
    ("auto_exercise", lambda: sa.Column("auto_exercise", sa.Boolean(), nullable=False, server_default=sa.false())),
)


def _have() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("scheduled_events")}


def upgrade() -> None:
    have = _have()
    missing = [column for name, column in COLUMNS if name not in have]
    if not missing:
        return
    # Batch mode: SQLite cannot add a column with a foreign key in place.
    with op.batch_alter_table("scheduled_events") as batch:
        for column in missing:
            batch.add_column(column())


def downgrade() -> None:
    have = _have()
    gone = [name for name, _ in reversed(COLUMNS) if name in have]
    if not gone:
        return
    # SQLite cannot drop a column with a foreign key in place; batch mode rebuilds the table.
    with op.batch_alter_table("scheduled_events") as batch:
        for name in gone:
            batch.drop_column(name)
