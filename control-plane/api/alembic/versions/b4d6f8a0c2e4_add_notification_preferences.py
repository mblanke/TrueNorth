"""notification preferences: a user's own switch for ticket email

Revision ID: b4d6f8a0c2e4
Revises: a3c5e7f9b1d2
Create Date: 2026-11-03 18:00:00.000000

#378: ticket notifications can also be emailed. ``notification_preferences`` holds each
user's own choice, one row per user, written only when they change it; no row means email
is on. Additive; no rows are written. Downgrade drops the table (everyone is back to the
default).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "b4d6f8a0c2e4"
down_revision: str | None = "a3c5e7f9b1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "notification_preferences" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "notification_preferences",
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("email_tickets", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("(CURRENT_TIMESTAMP)")
        ),
    )


def downgrade() -> None:
    op.drop_table("notification_preferences")
