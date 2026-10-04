"""add wiki (spaces, pages, revisions) and trouble tickets

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-10-04 12:00:00.000000

A Confluence-style wiki and Jira-style ticketing inside TrueNorth. Models live in
`app/models_wiki.py` and `app/models_tickets.py`. Each table is skipped when it
already exists, because the API's `create_all()` may have built it first in dev.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import GUID

# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    ]


def _deleted_at() -> sa.Column:
    return sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    if not _has_table("wiki_spaces"):
        op.create_table(
            "wiki_spaces",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("slug", sa.String(100), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("icon", sa.String(50), nullable=False, server_default="folder"),
            sa.Column("visibility", sa.String(10), nullable=False, server_default="all"),
            sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_by", GUID(), nullable=True),
            _deleted_at(),
            *_stamps(),
            sa.UniqueConstraint("tenant_id", "slug", name="uq_wiki_spaces_tenant_slug"),
        )
        op.create_index("ix_wiki_spaces_tenant_id", "wiki_spaces", ["tenant_id"])
        op.create_index("ix_wiki_spaces_deleted_at", "wiki_spaces", ["deleted_at"])

    if not _has_table("wiki_pages"):
        op.create_table(
            "wiki_pages",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("space_id", GUID(), sa.ForeignKey("wiki_spaces.id"), nullable=False),
            sa.Column("parent_id", GUID(), sa.ForeignKey("wiki_pages.id"), nullable=True),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("slug", sa.String(200), nullable=False),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("tags", sa.String(500), nullable=False, server_default=""),
            sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("author_id", GUID(), nullable=False),
            sa.Column("last_editor_id", GUID(), nullable=False),
            sa.Column("revision_number", sa.Integer(), nullable=False, server_default="1"),
            _deleted_at(),
            *_stamps(),
        )
        op.create_index("ix_wiki_pages_space_parent", "wiki_pages", ["space_id", "parent_id"])
        op.create_index("ix_wiki_pages_tenant", "wiki_pages", ["tenant_id"])
        op.create_index("ix_wiki_pages_deleted_at", "wiki_pages", ["deleted_at"])

    if not _has_table("wiki_revisions"):
        op.create_table(
            "wiki_revisions",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("page_id", GUID(), sa.ForeignKey("wiki_pages.id"), nullable=False),
            sa.Column("revision_number", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("editor_id", GUID(), nullable=False),
            sa.Column("edit_summary", sa.String(500), nullable=False, server_default=""),
            *_stamps(),
            sa.UniqueConstraint("page_id", "revision_number", name="uq_wiki_revisions_page_rev"),
        )
        op.create_index("ix_wiki_revisions_page_id", "wiki_revisions", ["page_id"])

    if not _has_table("support_queues"):
        op.create_table(
            "support_queues",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("slug", sa.String(100), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            _deleted_at(),
            *_stamps(),
            sa.UniqueConstraint("tenant_id", "slug", name="uq_support_queues_tenant_slug"),
        )
        op.create_index("ix_support_queues_tenant_id", "support_queues", ["tenant_id"])
        op.create_index("ix_support_queues_deleted_at", "support_queues", ["deleted_at"])

    if not _has_table("tickets"):
        op.create_table(
            "tickets",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("number", sa.Integer(), nullable=False),
            sa.Column("type", sa.String(20), nullable=False, server_default="incident"),
            sa.Column("subject", sa.String(500), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            sa.Column("priority", sa.String(20), nullable=False, server_default="medium"),
            sa.Column("category", sa.String(50), nullable=False, server_default="other"),
            sa.Column("labels", sa.String(500), nullable=False, server_default=""),
            sa.Column("queue_id", GUID(), sa.ForeignKey("support_queues.id"), nullable=True),
            sa.Column("reporter_id", GUID(), nullable=False),
            sa.Column("assignee_id", GUID(), nullable=True),
            sa.Column("range_id", GUID(), sa.ForeignKey("ranges.id"), nullable=True),
            sa.Column("exercise_id", GUID(), sa.ForeignKey("exercises.id"), nullable=True),
            sa.Column("board_order", sa.Float(), nullable=False, server_default="0"),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            _deleted_at(),
            *_stamps(),
            sa.UniqueConstraint("tenant_id", "number", name="uq_tickets_tenant_number"),
        )
        op.create_index("ix_tickets_tenant_status", "tickets", ["tenant_id", "status"])
        op.create_index("ix_tickets_tenant_reporter", "tickets", ["tenant_id", "reporter_id"])
        op.create_index("ix_tickets_tenant_assignee", "tickets", ["tenant_id", "assignee_id"])
        op.create_index("ix_tickets_deleted_at", "tickets", ["deleted_at"])

    if not _has_table("ticket_comments"):
        op.create_table(
            "ticket_comments",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("ticket_id", GUID(), sa.ForeignKey("tickets.id"), nullable=False),
            sa.Column("author_id", GUID(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("is_internal", sa.Boolean(), nullable=False, server_default=sa.false()),
            *_stamps(),
        )
        op.create_index("ix_ticket_comments_ticket_id", "ticket_comments", ["ticket_id"])

    if not _has_table("ticket_attachments"):
        op.create_table(
            "ticket_attachments",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("ticket_id", GUID(), sa.ForeignKey("tickets.id"), nullable=False),
            sa.Column("filename", sa.String(512), nullable=False),
            sa.Column("content_type", sa.String(120), nullable=False, server_default="application/octet-stream"),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("object_key", sa.String(600), nullable=False, server_default=""),
            sa.Column("uploaded_by", GUID(), nullable=False),
            *_stamps(),
        )
        op.create_index("ix_ticket_attachments_ticket_id", "ticket_attachments", ["ticket_id"])

    if not _has_table("ticket_activity"):
        op.create_table(
            "ticket_activity",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("tenant_id", GUID(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("ticket_id", GUID(), sa.ForeignKey("tickets.id"), nullable=False),
            sa.Column("actor_id", GUID(), nullable=False),
            sa.Column("field", sa.String(50), nullable=False),
            sa.Column("old_value", sa.String(500), nullable=False, server_default=""),
            sa.Column("new_value", sa.String(500), nullable=False, server_default=""),
            *_stamps(),
        )
        op.create_index("ix_ticket_activity_ticket_id", "ticket_activity", ["ticket_id"])


def downgrade() -> None:
    for name in (
        "ticket_activity",
        "ticket_attachments",
        "ticket_comments",
        "tickets",
        "support_queues",
        "wiki_revisions",
        "wiki_pages",
        "wiki_spaces",
    ):
        if _has_table(name):
            op.drop_table(name)
