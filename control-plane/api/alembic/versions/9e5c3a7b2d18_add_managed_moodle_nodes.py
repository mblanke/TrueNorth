"""add managed_moodle_nodes: which Moodle platforms are TrueNorth's own farm nodes

Revision ID: 9e5c3a7b2d18
Revises: 8d4b2f6a1c57
Create Date: 2026-10-10 18:00:00.000000

Written only by the installer (``install_cli register|manage``): a farm node's LTI
launches bind farm accounts by their locked TrueNorth id, and its server-side traffic may
reach a private address (app/moodle_farm/service.py). Additive; downgrade drops it. Nodes
the installer registered before this revision are marked on its next run.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "9e5c3a7b2d18"
down_revision: str | None = "8d4b2f6a1c57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "managed_moodle_nodes" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "managed_moodle_nodes",
        sa.Column("platform_id", GUID(), sa.ForeignKey("external_platforms.id"), primary_key=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("node", sa.String(64), nullable=False),
        sa.Column("marked_by", sa.String(32), nullable=False),
        sa.Column("marked_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    if "managed_moodle_nodes" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("managed_moodle_nodes")
