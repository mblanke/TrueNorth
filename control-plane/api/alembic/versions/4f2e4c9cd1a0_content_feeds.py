"""content feeds: release, end-of-life and vulnerability entries, the flags they raised, source status

Revision ID: 4f2e4c9cd1a0
Revises: 648ef1fe0b6c
Create Date: 2026-10-27 12:00:00.000000

ADR 0014 decision 25, issue #287 (``app/content_feeds``, ``worker/content_feeds``):

* ``content_feed_entries``: what endoflife.date, CISA KEV and the NVD say about the products
  the golden templates are built from, from the daily pull or a signed feed bundle;
* ``content_feed_flags``: each (content review item, entry) already flagged, so a flag is
  raised once and Mark reviewed clears it for good;
* ``content_feed_sources``: each source's last pull or import, last success, error, cursor;
* ``content_feed_settings``: one row, written when the sync first runs with feeds on: when
  they were enabled (older vulnerabilities are never flagged).

Additive; no rows are written (feeds are off until the installer turns them on). Downgrade
drops the four tables; the stale reasons the feeds raised stay on the review rows until
they are reviewed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "4f2e4c9cd1a0"
down_revision: str | None = "648ef1fe0b6c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "content_feed_entries" not in existing:
        op.create_table(
            "content_feed_entries",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("entry_key", sa.String(200), nullable=False, unique=True),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("products", sa.JSON(), nullable=False),
            sa.Column("at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("title", sa.String(500), nullable=False, server_default=""),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("origin", sa.String(16), nullable=False, server_default="online"),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_content_feed_entries_source", "content_feed_entries", ["source"])
    if "content_feed_flags" not in existing:
        op.create_table(
            "content_feed_flags",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("item_id", GUID(), sa.ForeignKey("content_review_items.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("entry_key", sa.String(200), nullable=False),
            sa.Column("raised_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("item_id", "entry_key", name="uq_content_feed_flag"),
        )
    if "content_feed_sources" not in existing:
        op.create_table(
            "content_feed_sources",
            sa.Column("source", sa.String(32), primary_key=True),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("entries", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cursor", sa.String(64), nullable=False, server_default=""),
            sa.Column("detail", sa.JSON(), nullable=False),
        )
    if "content_feed_settings" not in existing:
        op.create_table(
            "content_feed_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_table("content_feed_settings")
    op.drop_table("content_feed_sources")
    op.drop_table("content_feed_flags")
    op.drop_index("ix_content_feed_entries_source", table_name="content_feed_entries")
    op.drop_table("content_feed_entries")
