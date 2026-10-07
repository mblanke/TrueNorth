"""add scheduler_settings (ADR 0004 slice 2: admin-set over-capacity policy)

Revision ID: a4b5c6d7e8f9
Revises: a9b0c1d2e3f4
Create Date: 2026-10-05 15:00:00.000000

One row per key; the only key so far is `overcapacity_policy` (`block` or `warn`).
No row means the default, `block`, so nothing is seeded.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

# revision identifiers, used by Alembic.
revision: str = "a4b5c6d7e8f9"
down_revision: str | None = "a9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A dev deploy starts the API (DB_AUTO_CREATE -> create_all) before running Alembic,
    # so the table may already exist.
    if "scheduler_settings" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "scheduler_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
    )


def downgrade() -> None:
    if "scheduler_settings" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("scheduler_settings")
