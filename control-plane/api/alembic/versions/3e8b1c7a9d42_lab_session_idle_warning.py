"""lab session idle warning

Revision ID: 3e8b1c7a9d42
Revises: 57e2c4b65b98
Create Date: 2026-10-27 12:00:00.000000

Self-serve labs close after the idle limit (ADR 0014 §18, #288; ``LAB_IDLE_LIMIT_MINUTES``,
4 hours by default). ``idle_warned_at`` records that the Student was told the lab closes
soon, so the sweep tells them once; activity clears it. Additive, no data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3e8b1c7a9d42"
down_revision: str | None = "57e2c4b65b98"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("lab_sessions")}
    if "idle_warned_at" not in cols:
        op.add_column("lab_sessions", sa.Column("idle_warned_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("lab_sessions", "idle_warned_at")
