"""add provisioner_mode to hypervisor_connections

Revision ID: a1b2c3d4e5f6
Revises: e49a3ec63fcb
Create Date: 2026-06-01 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "e49a3ec63fcb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hypervisor_connections",
        sa.Column(
            "provisioner_mode",
            sa.String(length=20),
            nullable=False,
            server_default="api",
        ),
    )


def downgrade() -> None:
    op.drop_column("hypervisor_connections", "provisioner_mode")
