"""content clock: review state, dependency edges, theme tags and review cycles

Revision ID: c1465c4964a6
Revises: b62353ae80af
Create Date: 2026-10-22 18:00:00.000000

ADR 0014 decisions 22-24, issue #286 (``app/content_review``):

* ``content_review_items``: the review state of each course, module, lab, scenario, range
  template and golden template (owner, reviewer, themes, cycle, last review, due date,
  stale reasons, retired);
* ``content_review_edges``: what each item depends on, so staleness flows down;
* ``content_review_themes``: theme tags with a colour. The eight themes ADR 0014 decision 22
  names are added platform-wide; admins edit or delete them;
* ``content_review_cycles``: the review cycle per content type (90 to 120 days; none: 90).

Additive. The worker's periodic sync fills the items and edges from the content that
exists. Downgrade drops all four tables.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

revision: str = "c1465c4964a6"
down_revision: str | None = "b62353ae80af"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ADR 0014 decision 22's themes and the approved mockup's colours (the dot).
THEMES = (
    ("Blue team", "#3B5BDB"),
    ("Red team", "#E03131"),
    ("DFIR", "#7950F2"),
    ("Network", "#12A594"),
    ("ICS / OT", "#F59F00"),
    ("Cloud", "#D6336C"),
    ("Malware", "#2F9E44"),
    ("Platform", "#6B7280"),
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "content_review_items" not in existing:
        op.create_table(
            "content_review_items",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("item_type", sa.String(32), nullable=False),
            sa.Column("item_id", sa.String(64), nullable=False),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("owner_id", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("reviewer_id", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("theme_ids", sa.JSON(), nullable=False),
            sa.Column("cycle_days", sa.Integer(), nullable=True),
            sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_reviewed_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("stale_reasons", sa.JSON(), nullable=False),
            sa.Column("retired", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("retired_by", GUID(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("missing", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            *_timestamps(),
            sa.UniqueConstraint("item_type", "item_id", name="uq_content_review_item"),
        )
        op.create_index("ix_content_review_items_tenant_due", "content_review_items", ["tenant_id", "due_at"])
        op.create_index("ix_content_review_items_stale", "content_review_items", ["stale"])
    if "content_review_edges" not in existing:
        op.create_table(
            "content_review_edges",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("item_id", GUID(), sa.ForeignKey("content_review_items.id", ondelete="CASCADE"), nullable=False),
            sa.Column("depends_on_id", GUID(), sa.ForeignKey("content_review_items.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("derived", sa.Boolean(), nullable=False),
            sa.UniqueConstraint("item_id", "depends_on_id", name="uq_content_review_edge"),
        )
        op.create_index("ix_content_review_edges_depends_on", "content_review_edges", ["depends_on_id"])
    if "content_review_themes" not in existing:
        themes = op.create_table(
            "content_review_themes",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("name", sa.String(60), nullable=False),
            sa.Column("colour", sa.String(7), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            *_timestamps(),
        )
        op.create_index("ix_content_review_themes_tenant", "content_review_themes", ["tenant_id"])
        op.bulk_insert(themes, [
            {"id": uuid.uuid4(), "tenant_id": None, "name": name, "colour": colour, "ordinal": i}
            for i, (name, colour) in enumerate(THEMES)
        ])
    if "content_review_cycles" not in existing:
        op.create_table(
            "content_review_cycles",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("item_type", sa.String(32), nullable=False),
            sa.Column("days", sa.Integer(), nullable=False),
            *_timestamps(),
        )
        op.create_index("ix_content_review_cycles_tenant_type", "content_review_cycles", ["tenant_id", "item_type"],
                        unique=True)
        # NULLs are distinct in a unique index: one platform-wide row per type needs its own.
        op.create_index("uq_content_review_cycles_platform", "content_review_cycles", ["item_type"], unique=True,
                        postgresql_where=sa.text("tenant_id IS NULL"), sqlite_where=sa.text("tenant_id IS NULL"))


def downgrade() -> None:
    op.drop_index("uq_content_review_cycles_platform", table_name="content_review_cycles")
    op.drop_index("ix_content_review_cycles_tenant_type", table_name="content_review_cycles")
    op.drop_table("content_review_cycles")
    op.drop_index("ix_content_review_themes_tenant", table_name="content_review_themes")
    op.drop_table("content_review_themes")
    op.drop_index("ix_content_review_edges_depends_on", table_name="content_review_edges")
    op.drop_table("content_review_edges")
    op.drop_index("ix_content_review_items_stale", table_name="content_review_items")
    op.drop_index("ix_content_review_items_tenant_due", table_name="content_review_items")
    op.drop_table("content_review_items")
