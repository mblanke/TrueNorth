"""add network_reservations: collision-free addresses and VLANs on shared networks

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-10-04 23:30:00.000000

Additive: a new table; nothing reads it until a consumer (noise deploy, vSphere uplink
and VLAN allocation) is switched to it. Existing ranges' addresses live in
`ranges.provisioner_output` and are not backfilled; a consumer that adopts the table
records them as it next touches each range. Model: `app/models_network.py`.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "c0d1e2f3a4b5"
down_revision: str | None = "b9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "network_reservations" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "network_reservations",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("value", sa.String(64), nullable=False),
        sa.Column("holder", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("domain", "kind", "value", name="uq_network_reservations_value"),
        sa.UniqueConstraint("range_id", "kind", "holder", name="uq_network_reservations_holder"),
    )


def downgrade() -> None:
    op.drop_table("network_reservations")
