"""add range_leases: one worker execution acts on a range at a time

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-10-05 14:00:00.000000

Additive: a new table the worker writes while a range task runs (worker/fencing.py).
Empty at rest; nothing to backfill. Downgrade drops it; the previous code does not read it.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "e2f3a4b5c6d7"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "range_leases" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "range_leases",
        sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("holder", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("range_leases")
