"""add range description + supporting documents

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-21 12:00:00.000000

A range had no way to say what it was for. `description` holds operator-facing
markdown (typed or imported from a text file); `range_documents` keeps
supporting files with their original bytes in object storage.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from app.models import GUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Tolerant of a database that Base.metadata.create_all has already touched:
    # the API calls it on every boot, so in dev the new table exists before this
    # migration ever runs, and a bare create_table would abort the upgrade.
    if not _has_column("ranges", "description"):
        with op.batch_alter_table("ranges") as batch:
            batch.add_column(sa.Column("description", sa.Text(), nullable=False, server_default=""))

    if _has_table("range_documents"):
        return

    op.create_table(
        "range_documents",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id"), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False, server_default=""),
        sa.Column("minio_key", sa.String(600), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_range_docs_range", "range_documents", ["range_id"])
    op.create_index("ix_range_docs_tenant", "range_documents", ["tenant_id"])


def downgrade() -> None:
    if _has_table("range_documents"):
        op.drop_table("range_documents")
    if _has_column("ranges", "description"):
        with op.batch_alter_table("ranges") as batch:
            batch.drop_column("description")
