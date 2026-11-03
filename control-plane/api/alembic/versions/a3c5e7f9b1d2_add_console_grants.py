"""console relay: console grants

Revision ID: a3c5e7f9b1d2
Revises: 9d4e7a2b5c18
Create Date: 2026-11-03 12:00:00.000000

#366 (``app/console_relay``): a browser reaches a VM's console only through the API's
``/ws/console/<grant>``. ``console_grants`` holds the grants: one user, one VM, about five
minutes, one live socket at a time. Only the SHA-256 of the URL token is stored.

Additive; no rows are written. Downgrade drops the table (open grants lapse; consoles are
asked for again).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "a3c5e7f9b1d2"
down_revision: str | None = "9d4e7a2b5c18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "console_grants" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "console_grants",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False),
        sa.Column("vm_id", sa.String(255), nullable=False),
        sa.Column("vm_name", sa.String(255), nullable=False),
        sa.Column("view_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("observed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("audience", sa.String(16), nullable=False, server_default="student"),
        sa.Column("class_id", GUID(), nullable=True),
        sa.Column("event_id", GUID(), nullable=True),
        sa.Column("lab_session_id", GUID(), nullable=True),
        sa.Column("dials", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("live_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("live_by", sa.String(32), nullable=True),
    )
    op.create_index("ix_console_grants_expires", "console_grants", ["expires_at"])
    op.create_index("ix_console_grants_user", "console_grants", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_console_grants_user", table_name="console_grants")
    op.drop_index("ix_console_grants_expires", table_name="console_grants")
    op.drop_table("console_grants")
