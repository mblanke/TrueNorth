"""add range diagram_json column

Revision ID: f6a1b2c3d4e5
Revises: e5f6a1b2c3d4
Create Date: 2026-04-17 20:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a1b2c3d4e5"
down_revision: str | None = "e5f6a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ranges",
        sa.Column("diagram_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ranges", "diagram_json")
