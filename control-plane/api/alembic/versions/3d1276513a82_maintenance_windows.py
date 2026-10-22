"""maintenance windows, and the window bypass on range operations

Revision ID: 3d1276513a82
Revises: e0f1a2b3c4d5
Create Date: 2026-10-21 23:30:00.000000

Range certification S7 (ADR 0013 §7). ``maintenance_windows``: periods in which
provision, reset/restore and normal destroy are refused, per tenant or platform-wide
(``tenant_id`` null). ``range_operations.window_bypass``: set on a teardown that runs
inside a window anyway (an emergency, a booking that ended), so the worker's re-check at
the task's entry lets it through. Additive; existing operations get null (normal).

Downgrade drops both.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "3d1276513a82"
down_revision: str | None = "e0f1a2b3c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "maintenance_windows" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "maintenance_windows",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("reason", sa.String(500), nullable=False),
            sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("scope", sa.String(16), nullable=False, server_default="all"),
            sa.Column("range_ids", sa.JSON(), nullable=False),
            sa.Column("tags", sa.JSON(), nullable=False),
            sa.Column("created_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("updated_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_maintenance_windows_tenant", "maintenance_windows", ["tenant_id"])
        op.create_index("ix_maintenance_windows_ends_at", "maintenance_windows", ["ends_at"])
    columns = {c["name"] for c in sa.inspect(bind).get_columns("range_operations")}
    if "window_bypass" not in columns:
        with op.batch_alter_table("range_operations") as batch:
            batch.add_column(sa.Column("window_bypass", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("range_operations") as batch:
        batch.drop_column("window_bypass")
    op.drop_index("ix_maintenance_windows_ends_at", table_name="maintenance_windows")
    op.drop_index("ix_maintenance_windows_tenant", table_name="maintenance_windows")
    op.drop_table("maintenance_windows")
