"""Acknowledgement versions and signatures (ADR 0014 decision 3; issue #279).

New tables go in their section, never into the shared ``app.models`` (ADR 0003).

- ``acknowledgement_versions``: the rules-of-use text, in Markdown, platform-wide (the
  ADR's assumption: an admin of the platform tenant edits it and it applies to every
  tenant). A row is a draft until it is published; publishing gives it the next
  ``version`` number and freezes it. A correction is a new version.
- ``acknowledgement_signatures``: who signed which version, when, from where. ``tenant_id``
  is the signer's tenant when they signed, so an export by tenant does not move when a
  user later changes tenant.

Nothing here is deleted by the API (ADR 0010).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin


class AcknowledgementVersion(TimestampMixin, Base):
    __tablename__ = "acknowledgement_versions"
    __table_args__ = (UniqueConstraint("version", name="uq_acknowledgement_versions_version"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    # None while a draft; 1, 2, 3... in publication order once published.
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id"), nullable=True)

    @property
    def is_published(self) -> bool:
        return self.published_at is not None


class AcknowledgementSignature(Base):
    __tablename__ = "acknowledgement_signatures"
    __table_args__ = (
        Index("ix_ack_signatures_user_version", "user_id", "version_id", "signed_at"),
        Index("ix_ack_signatures_tenant_signed", "tenant_id", "signed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("acknowledgement_versions.id"), nullable=False
    )
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
