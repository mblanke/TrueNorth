"""golden_image_builds: registered builds of a golden image and the promoted one (ADR 0013 §2)

Revision ID: ce2a35194081
Revises: 0a2276639b3c
Create Date: 2026-10-21 12:00:00.000000

One row per build the template kit registers: build id, checksum, build date, ISO
checksum and the build's copy name per range host. At most one build per image is
promoted (partial unique index); ranges clone only that build. Additive: existing images
have no builds until the kit registers one, and ranges then need a promotion (or the
transition flag TN_TEMPLATE_ALLOW_UNPROMOTED). Downgrade drops the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "ce2a35194081"
down_revision: str | None = "0a2276639b3c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "golden_image_builds" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "golden_image_builds",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("golden_image_id", GUID(), sa.ForeignKey("golden_images.id", ondelete="CASCADE"), nullable=False),
        sa.Column("build_id", sa.String(120), nullable=False),
        sa.Column("template_name", sa.String(120), nullable=False, server_default=""),
        sa.Column("checksum", sa.String(128), nullable=False),
        sa.Column("build_date", sa.Date(), nullable=True),
        sa.Column("iso_checksum", sa.String(160), nullable=False, server_default=""),
        sa.Column("copies", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("registered_by", sa.String(255), nullable=False, server_default=""),
        sa.Column("promoted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_by", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("golden_image_id", "build_id", name="uq_golden_image_build"),
    )
    op.create_index("ix_golden_image_builds_image", "golden_image_builds", ["golden_image_id"])
    op.create_index("uq_golden_image_builds_promoted", "golden_image_builds", ["golden_image_id"], unique=True,
                    postgresql_where=sa.text("promoted"), sqlite_where=sa.text("promoted"))


def downgrade() -> None:
    op.drop_index("uq_golden_image_builds_promoted", table_name="golden_image_builds")
    op.drop_index("ix_golden_image_builds_image", table_name="golden_image_builds")
    op.drop_table("golden_image_builds")
