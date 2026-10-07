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

COLUMNS = (
    ("scenario_id", lambda: sa.Column("scenario_id", GUID(), sa.ForeignKey("scenarios.id"), nullable=True)),
    ("exercise_id", lambda: sa.Column("exercise_id", GUID(), sa.ForeignKey("exercises.id"), nullable=True)),
    ("auto_exercise", lambda: sa.Column("auto_exercise", sa.Boolean(), nullable=False, server_default=sa.false())),
)


def _have() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("scheduled_events")}


def upgrade() -> None:
    have = _have()
    for name, column in COLUMNS:
        if name not in have:
            op.add_column("scheduled_events", column())


def downgrade() -> None:
    have = _have()
    for name, _ in reversed(COLUMNS):
        if name in have:
            op.drop_column("scheduled_events", name)
