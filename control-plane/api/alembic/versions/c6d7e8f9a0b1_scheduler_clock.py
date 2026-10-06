"""scheduler clock: auto_provisioned and reminded_at (ADR 0004 slice 4)

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-10-06 14:00:00.000000

Skipped when present: b0c1d2e3f4a5 builds `scheduled_events` from the live ORM on an
empty database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6d7e8f9a0b1"
down_revision: str | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("scheduled_events")}


def upgrade() -> None:
    have = _columns()
    if "auto_provisioned" not in have:
        op.add_column(
            "scheduled_events",
            sa.Column("auto_provisioned", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "reminded_at" not in have:
        op.add_column("scheduled_events", sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    have = _columns()
    for name in ("reminded_at", "auto_provisioned"):
        if name in have:
            op.drop_column("scheduled_events", name)
