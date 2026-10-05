"""range power: `stopping` and `starting` range states

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-10-05 10:00:00.000000

Stop and start are range operations now (app/range_ops.py): the range is `stopping` or
`starting` while the worker powers its VMs. Additive on PostgreSQL (the native enum
gains two values); SQLite stores the state as text. Downgrade puts any range caught
mid-operation back where its VMs were before it (`ready` / `stopped`); PostgreSQL cannot
drop an enum value, so the two values stay in the type, unused.

"""

from collections.abc import Sequence

from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "c0d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE rangestate ADD VALUE IF NOT EXISTS 'stopping'")
        op.execute("ALTER TYPE rangestate ADD VALUE IF NOT EXISTS 'starting'")


def downgrade() -> None:
    op.execute("UPDATE ranges SET state = 'ready' WHERE CAST(state AS TEXT) = 'stopping'")
    op.execute("UPDATE ranges SET state = 'stopped' WHERE CAST(state AS TEXT) = 'starting'")
