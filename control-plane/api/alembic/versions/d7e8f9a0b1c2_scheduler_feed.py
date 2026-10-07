"""scheduler feed: per-user feed tokens and event SEQUENCE (ADR 0004 slice 5)

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-10-06 18:00:00.000000

`sequence` is skipped when present: b0c1d2e3f4a5 builds `scheduled_events` from the
live ORM on an empty database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

# revision identifiers, used by Alembic.
revision: str = "d7e8f9a0b1c2"
down_revision: str | None = "c6d7e8f9a0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "sequence" not in {c["name"] for c in inspector.get_columns("scheduled_events")}:
        op.add_column("scheduled_events", sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"))
    if "scheduler_feed_tokens" not in inspector.get_table_names():
        op.create_table(
            "scheduler_feed_tokens",
            sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "scheduler_feed_tokens" in inspector.get_table_names():
        op.drop_table("scheduler_feed_tokens")
    if "sequence" in {c["name"] for c in inspector.get_columns("scheduled_events")}:
        op.drop_column("scheduled_events", "sequence")
