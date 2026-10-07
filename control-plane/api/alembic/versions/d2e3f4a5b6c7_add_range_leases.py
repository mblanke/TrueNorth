"""add range leases

Revision ID: d2e3f4a5b6c7
Revises: c1e2f3a4b5c6
Create Date: 2026-10-06 20:00:00.000000

One row per range a worker task is acting on (app/range_leases, worker/fencing.py), so
two deliveries of one range task never reach the hypervisor at once. Additive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "c1e2f3a4b5c6"
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
