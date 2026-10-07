"""lab session lease holder

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-10-07 10:30:00.000000

The lab session lease had no owner: a run that outlived its 120 s lease (a slow probe, a
stalled process) could still commit, send tasks and release a lease another process had
taken since (CR1-16; the limit R1a recorded). ``lease_holder`` names the run that holds
it; commits, sends and releases now happen only while it still does. Additive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "b6c7d8e9f0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("lab_sessions")}
    if "lease_holder" not in cols:
        op.add_column("lab_sessions", sa.Column("lease_holder", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("lab_sessions", "lease_holder")
