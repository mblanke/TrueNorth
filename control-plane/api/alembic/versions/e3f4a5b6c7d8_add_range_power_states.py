"""add range states stopping and starting

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-10-06 21:00:00.000000

/stop and /start now power the range's VMs (CR1-05): the API records the request by
moving the range into ``stopping`` / ``starting`` and the worker writes ``stopped`` /
``running`` once the hypervisor did it (worker/power_tasks.py). Additive: two values on
the PostgreSQL ``rangestate`` enum; SQLite stores the enum as VARCHAR.

Downgrade: PostgreSQL cannot drop an enum value, so the values stay. Ranges in them are
moved to what the previous code would have shown (``stopping`` -> ``stopped``,
``starting`` -> ``running``, what it set without asking the hypervisor).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: str | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE rangestate ADD VALUE IF NOT EXISTS 'stopping' AFTER 'running'")
        op.execute("ALTER TYPE rangestate ADD VALUE IF NOT EXISTS 'starting' AFTER 'stopped'")


def downgrade() -> None:
    op.execute("UPDATE ranges SET state = 'stopped' WHERE CAST(state AS TEXT) = 'stopping'")
    op.execute("UPDATE ranges SET state = 'running' WHERE CAST(state AS TEXT) = 'starting'")
