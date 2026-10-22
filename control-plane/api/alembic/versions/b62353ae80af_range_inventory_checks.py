"""range inventory checks: teardown verification and drift

Revision ID: b62353ae80af
Revises: 3d1276513a82
Create Date: 2026-10-22 12:00:00.000000

Range certification S5 (ADR 0013 §5, issue #243). ``range_inventory_checks``: the latest
teardown verification and drift check per range (one row per range and kind), written by
the worker, read by the range page and the confirm-by-moid flow. The history is in the
Range Certification Record. Additive.

Downgrade drops the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "b62353ae80af"
down_revision: str | None = "3d1276513a82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "range_inventory_checks" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "range_inventory_checks",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", GUID(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("range_id", "kind", name="uq_range_inventory_checks_kind"),
    )
    op.create_index("ix_range_inventory_checks_tenant", "range_inventory_checks", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_range_inventory_checks_tenant", table_name="range_inventory_checks")
    op.drop_table("range_inventory_checks")
