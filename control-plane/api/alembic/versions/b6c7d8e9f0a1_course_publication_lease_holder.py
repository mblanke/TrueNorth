"""course publication lease holder

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-10-07 09:30:00.000000

The publication lease had no owner: each step renewed ``lease_until`` unconditionally, so
a step that outlived the lease let a second process (startup resume, a retry) take the
job while the first kept writing over it (CR1-15). ``lease_holder`` names the process
run that holds it; every step renews only its own (app/course_publishing). Additive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6c7d8e9f0a1"
down_revision: str | None = "a5b6c7d8e9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("course_publications")}
    if "lease_holder" not in cols:
        op.add_column("course_publications", sa.Column("lease_holder", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("course_publications", "lease_holder")
