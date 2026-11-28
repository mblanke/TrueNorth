# Author: Marc-Andre Blanke
# Created: 2026-11-28T03:36:45Z
"""range state events

Revision ID: f8b0d2c4e6a7
Revises: e7a9c1b3d5f6
Create Date: 2026-11-28 03:36:45.000000

Usage S2 (ADR 0021). ``range_state_events``: one append-only row per change of a range's
state, written by the worker, the API and the hourly reconciler, so usage can tell when a
range's VMs were running, stopped or gone. No foreign keys: the history outlives the
range. Additive; existing ranges get their first row from the reconciler. Downgrade drops it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "f8b0d2c4e6a7"
down_revision: str | None = "e7a9c1b3d5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "range_state_events" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "range_state_events",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("tenant_id", GUID(), nullable=True),
        sa.Column("range_id", GUID(), nullable=False),
        sa.Column("from_state", sa.String(16), nullable=True),
        sa.Column("to_state", sa.String(16), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
    )
    op.create_index("ix_range_state_events_range_at", "range_state_events", ["range_id", "at"])
    op.create_index("ix_range_state_events_tenant_at", "range_state_events", ["tenant_id", "at"])


def downgrade() -> None:
    op.drop_index("ix_range_state_events_tenant_at", table_name="range_state_events")
    op.drop_index("ix_range_state_events_range_at", table_name="range_state_events")
    op.drop_table("range_state_events")
