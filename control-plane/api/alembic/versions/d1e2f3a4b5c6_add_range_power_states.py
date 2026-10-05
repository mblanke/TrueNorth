"""add range states stopping and starting: power operations are fenced like provision

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-10-05 10:00:00.000000

Additive: two values on the `rangestate` enum (PostgreSQL; SQLite stores the enum as
VARCHAR with no CHECK constraint). /stop and /start are now range operations and move the
range into `stopping` / `starting` until the worker reports (app/range_states.py).

Downgrade: PostgreSQL cannot drop an enum value, so the values stay; ranges in them are
moved to the state the previous code would have shown (`stopping` -> `stopped`,
`starting` -> `running`, which is what it set optimistically), and their in-flight
stop/start operations are closed as abandoned, since the previous code does not know those
actions and would otherwise treat the range as busy for good. Upgrading again is a no-op
for the type (IF NOT EXISTS).

"""

from collections.abc import Sequence

from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "c0d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE rangestate ADD VALUE IF NOT EXISTS 'stopping' AFTER 'running'")
        op.execute("ALTER TYPE rangestate ADD VALUE IF NOT EXISTS 'starting' AFTER 'stopped'")


def downgrade() -> None:
    op.execute("UPDATE ranges SET state = 'stopped' WHERE CAST(state AS TEXT) = 'stopping'")
    op.execute("UPDATE ranges SET state = 'running' WHERE CAST(state AS TEXT) = 'starting'")
    op.execute(
        "UPDATE range_operations SET status = 'failed', "
        "error = '{\"code\": \"abandoned\", \"message\": \"schema downgraded below power operations\"}' "
        "WHERE action IN ('stop', 'start') AND status IN ('pending', 'dispatched')"
    )
