# Author: Marc-Andre Blanke
# Created: 2026-11-28T02:58:11Z
"""rate cards

Revision ID: e7a9c1b3d5f6
Revises: d6f8b0a2c4e5
Create Date: 2026-11-28 02:58:11.000000

Usage and cost S1. ``rate_cards``: what one hour of allocated vCPU, GB of RAM, GB of disk
and one VM cost, from ``effective_from`` on, per tenant or platform-wide (``tenant_id``
null). Additive; no existing table changes. Downgrade drops it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "e7a9c1b3d5f6"
down_revision: str | None = "d6f8b0a2c4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "rate_cards" in sa.inspect(bind).get_table_names():
        return
    rate = sa.Numeric(14, 6)
    op.create_table(
        "rate_cards",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="CAD"),
        sa.Column("per_vcpu_hour", rate, nullable=False, server_default="0"),
        sa.Column("per_gb_ram_hour", rate, nullable=False, server_default="0"),
        sa.Column("per_gb_disk_hour", rate, nullable=False, server_default="0"),
        sa.Column("per_vm_hour", rate, nullable=False, server_default="0"),
        sa.Column("notes", sa.String(1000), nullable=False, server_default=""),
        sa.Column("created_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_rate_cards_tenant_from", "rate_cards", ["tenant_id", "effective_from"])


def downgrade() -> None:
    op.drop_index("ix_rate_cards_tenant_from", table_name="rate_cards")
    op.drop_table("rate_cards")
