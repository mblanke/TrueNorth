"""network_reservations: external reservations (no range, no tenant)

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-10-21 12:00:00.000000

Range certification S1. Before it allocates VLANs the worker reads every port group on
the range switch and records the VLANs used outside TrueNorth as reservations with no
range and no tenant (holder ``external:<source>:<port group>``), so the allocator never
hands them out (worker/range_alloc.record_external). ``range_id`` and ``tenant_id``
become nullable; existing rows are untouched.

Downgrade deletes the external rows (the next allocation after an upgrade records them
again from the live switch) and makes both columns NOT NULL again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "e0f1a2b3c4d5"
down_revision: str | None = "d9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "network_reservations"


def upgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.alter_column("range_id", existing_type=GUID(), nullable=True)
        batch.alter_column("tenant_id", existing_type=GUID(), nullable=True)


def downgrade() -> None:
    nr = sa.table(TABLE, sa.column("range_id"), sa.column("tenant_id"))
    op.execute(nr.delete().where(sa.or_(nr.c.range_id.is_(None), nr.c.tenant_id.is_(None))))
    with op.batch_alter_table(TABLE) as batch:
        batch.alter_column("range_id", existing_type=GUID(), nullable=False)
        batch.alter_column("tenant_id", existing_type=GUID(), nullable=False)
