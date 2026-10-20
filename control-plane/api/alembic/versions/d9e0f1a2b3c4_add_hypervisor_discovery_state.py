"""hypervisor_discovery_state: scheduled rediscovery bookkeeping per connection

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-10-20 12:00:00.000000

One row per hypervisor connection (app/infra_discovery): when its hosts were last
discovered successfully, the last attempt and its error, and the lease that keeps one
discovery per connection at a time. Additive; existing connections get a row on their
first discovery after the upgrade. Downgrade drops the table: the next discovery starts
the record again, and the inventory itself (hypervisor_nodes) is untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "d9e0f1a2b3c4"
down_revision: str | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "hypervisor_discovery_state" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "hypervisor_discovery_state",
        sa.Column(
            "connection_id",
            GUID(),
            sa.ForeignKey("hypervisor_connections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("lease_holder", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("hypervisor_discovery_state")
