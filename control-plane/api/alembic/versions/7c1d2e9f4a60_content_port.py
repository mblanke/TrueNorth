"""content port: imported bundles and the items they brought

Revision ID: 7c1d2e9f4a60
Revises: 3e8b1c7a9d42
Create Date: 2026-10-27 18:00:00.000000

Owner decision 2026-10-27 (v1.2.0 GA; ``app/content_port``, docs/content-port.md): proven
courses, range templates and scenarios move between sites as a signed bundle. "Proven" is
computed, never stored; these two tables hold only what an import must remember:

* ``content_port_imports``: each bundle imported (its id is unique: a replay is refused),
  the key that signed it, the hash of what the signature covers, where it landed;
* ``content_port_items``: each local item an import created or updated, the origin item it
  came from, the origin's content hash, when its bundle was made (an older one is a
  downgrade), its local fingerprint after the import, and the origin's proof summary.

Additive; no rows are written. Downgrade drops both tables; the imported content stays.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "7c1d2e9f4a60"
down_revision: str | None = "3e8b1c7a9d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "content_port_imports" not in existing:
        op.create_table(
            "content_port_imports",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("bundle_id", GUID(), nullable=False),
            sa.Column("key_id", sa.String(16), nullable=False),
            sa.Column("signed_sha256", sa.String(64), nullable=False),
            sa.Column("exported_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("imported_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("shared", sa.Boolean(), nullable=False),
            sa.Column("summary", sa.JSON(), nullable=False),
            sa.UniqueConstraint("bundle_id", name="uq_content_port_imports_bundle"),
        )
    if "content_port_items" not in existing:
        op.create_table(
            "content_port_items",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("item_type", sa.String(32), nullable=False),
            sa.Column("local_id", sa.String(64), nullable=False),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("origin_id", sa.String(64), nullable=False),
            sa.Column("origin_key_id", sa.String(16), nullable=False),
            sa.Column("origin_content_sha256", sa.String(64), nullable=False),
            sa.Column("exported_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("import_id", GUID(), sa.ForeignKey("content_port_imports.id"), nullable=False),
            sa.Column("local_fingerprint", sa.String(64), nullable=False),
            sa.Column("proof", sa.JSON(), nullable=False),
            sa.Column("golden_builds", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("item_type", "local_id", name="uq_content_port_items_local"),
        )
        op.create_index("ix_content_port_items_origin", "content_port_items", ["item_type", "origin_id"])


def downgrade() -> None:
    op.drop_index("ix_content_port_items_origin", table_name="content_port_items")
    op.drop_table("content_port_items")
    op.drop_table("content_port_imports")
