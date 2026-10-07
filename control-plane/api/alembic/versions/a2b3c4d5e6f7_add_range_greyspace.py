"""add range greyspace

Revision ID: a2b3c4d5e6f7
Revises: c7d8e9f0a1b2
Create Date: 2026-10-07 12:00:00.000000

One row per range with a Greyspace (simulated internet) block attached (app/greyspace,
ADR 0007): the block, its corpus tier, and what the worker last recorded. Additive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "a2b3c4d5e6f7"
down_revision: str | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "range_greyspace" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "range_greyspace",
        sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("block", sa.JSON(), nullable=False),
        sa.Column("corpus_tier", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("attached_by", GUID(), nullable=True),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_range_greyspace_tenant", "range_greyspace", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_range_greyspace_tenant", table_name="range_greyspace")
    op.drop_table("range_greyspace")
