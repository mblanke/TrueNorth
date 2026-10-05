"""add range_operations: durable record of each requested range action

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-10-04 23:00:00.000000

Additive only: a new table, no change to `ranges`. Existing ranges have no operations;
nothing needs backfilling, and code that does not know the table keeps working.
Model: `app/models_range_ops.py`. Skipped when the table exists (dev `create_all()`).

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "b9c0d1e2f3a4"
down_revision: str | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "range_operations" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "range_operations",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("requested_by", GUID(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("task_id", sa.String(255), nullable=True),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("range_id", "generation", name="uq_range_operations_generation"),
        sa.UniqueConstraint("range_id", "idempotency_key", name="uq_range_operations_idempotency"),
    )
    op.create_index("ix_range_operations_status", "range_operations", ["status"])


def downgrade() -> None:
    op.drop_index("ix_range_operations_status", table_name="range_operations")
    op.drop_table("range_operations")
