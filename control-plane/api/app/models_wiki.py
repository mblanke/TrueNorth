"""TrueNorth Range - Wiki / knowledge base ORM models.

Kept out of ``models.py`` so the wiki section can change without touching the shared
model file; ``models.py`` re-exports these at its foot so ``create_all``, Alembic and
the static guards still see them.

Spaces hold a tree of markdown pages. Every save of a page writes a ``WikiRevision``;
``WikiPage.revision_number`` is the optimistic-concurrency token an editor must send
back, so two people editing the same page cannot silently overwrite each other.

Author / editor ids are not foreign keys: with AUTH_DISABLED the dev identity has no
``users`` row, and a page must still be saveable.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import GUID, SoftDeleteMixin, TimestampMixin

WIKI_VISIBILITY = ("all", "staff")


class WikiSpace(TimestampMixin, SoftDeleteMixin, Base):
    """A top-level wiki area, e.g. "Range Ops runbooks" or "Student handbook"."""

    __tablename__ = "wiki_spaces"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_wiki_spaces_tenant_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(50), default="folder")
    # "all" — every role can read it; "staff" — hidden from students and observers.
    visibility: Mapped[str] = mapped_column(String(10), default="all")
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)


class WikiPage(TimestampMixin, SoftDeleteMixin, Base):
    """One markdown page; ``parent_id`` builds the page tree inside a space."""

    __tablename__ = "wiki_pages"
    __table_args__ = (
        Index("ix_wiki_pages_space_parent", "space_id", "parent_id"),
        Index("ix_wiki_pages_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    space_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("wiki_spaces.id"), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("wiki_pages.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(String(500), default="")  # comma-separated
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    author_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    last_editor_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, default=1)


class WikiRevision(TimestampMixin, Base):
    """An immutable snapshot of a page as it was after one save."""

    __tablename__ = "wiki_revisions"
    __table_args__ = (UniqueConstraint("page_id", "revision_number", name="uq_wiki_revisions_page_rev"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    page_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("wiki_pages.id"), nullable=False, index=True)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    editor_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    edit_summary: Mapped[str] = mapped_column(String(500), default="")
