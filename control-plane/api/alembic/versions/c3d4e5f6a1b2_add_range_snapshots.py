"""add range snapshots table

Revision ID: c3d4e5f6a1b2
Revises: b2c3d4e5f6a1
Create Date: 2026-02-26 06:02:00.000000
"""

import os
import sys
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from app.models import GUID

revision: str = "c3d4e5f6a1b2"
down_revision: str | None = "b2c3d4e5f6a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "range_snapshots",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("snapshot_type", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("vm_snapshots_json", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("created_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_range_snapshots_range_created", "range_snapshots", ["range_id", sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("ix_range_snapshots_range_created", table_name="range_snapshots")
    op.drop_table("range_snapshots")
