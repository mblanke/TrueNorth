"""add golden_images template library registry

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-19 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.models import GUID

# revision identifiers, used by Alembic.
revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "golden_images",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("catalogue_id", sa.String(64), nullable=False),
        sa.Column("os_family", sa.String(40), nullable=False, server_default=""),
        sa.Column("version", sa.String(60), nullable=False, server_default=""),
        sa.Column("role", sa.String(160), nullable=False, server_default=""),
        sa.Column("hypervisor", sa.String(20), nullable=False, server_default="vsphere"),
        sa.Column("template_name", sa.String(120), nullable=False, server_default=""),
        sa.Column("datastore", sa.String(120), nullable=False, server_default=""),
        sa.Column("os_aliases", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("sensor_baked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("build_status", sa.String(20), nullable=False, server_default="planned"),
        sa.Column("checksum", sa.String(128), nullable=False, server_default=""),
        sa.Column("golden_gb", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("catalogue_id", "hypervisor", name="uq_image_hypervisor"),
    )
    op.create_index("ix_golden_hypervisor", "golden_images", ["hypervisor"])


def downgrade() -> None:
    op.drop_index("ix_golden_hypervisor", table_name="golden_images")
    op.drop_table("golden_images")
