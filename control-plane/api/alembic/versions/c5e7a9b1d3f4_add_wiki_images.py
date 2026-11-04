"""wiki images

Revision ID: c5e7a9b1d3f4
Revises: b4d6f8a0c2e4
Create Date: 2026-11-04 09:00:00.000000

#379: images uploaded to a wiki page. ``wiki_images`` holds the metadata; the bytes are in
object storage. Additive; no rows are written. Downgrade drops the table (the stored objects
are left in the bucket).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "c5e7a9b1d3f4"
down_revision: str | None = "b4d6f8a0c2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "wiki_images" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "wiki_images",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("page_id", GUID(), sa.ForeignKey("wiki_pages.id"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(50), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(300), nullable=False, server_default=""),
        sa.Column("uploaded_by", GUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_wiki_images_page", "wiki_images", ["page_id"])
    op.create_index("ix_wiki_images_deleted_at", "wiki_images", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_wiki_images_deleted_at", table_name="wiki_images")
    op.drop_index("ix_wiki_images_page", table_name="wiki_images")
    op.drop_table("wiki_images")
