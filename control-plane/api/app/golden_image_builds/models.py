"""Golden-image builds and the promoted one (ADR 0013 §2).

One row per build of a golden image that the template kit registered: the Packer build
id, the build's checksum, the build date, the install ISO's checksum and the name of the
build's copy on each range host (``<id>@<host>-<yyyymmdd>``, ADR 0012 §6). At most one
build per image is ``promoted``; ranges clone only that build (the worker's mirror is
``worker/tables.py``). Promoting is an audited admin action (``service.promote``).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin


class GoldenImageBuild(TimestampMixin, Base):
    """A registered build of a golden image; ``promoted`` marks the one ranges use."""

    __tablename__ = "golden_image_builds"
    __table_args__ = (
        UniqueConstraint("golden_image_id", "build_id", name="uq_golden_image_build"),
        Index("ix_golden_image_builds_image", "golden_image_id"),
        # One promoted build per image, enforced by the database too.
        Index("uq_golden_image_builds_promoted", "golden_image_id", unique=True,
              postgresql_where=text("promoted"), sqlite_where=text("promoted")),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    golden_image_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("golden_images.id", ondelete="CASCADE"), nullable=False)
    build_id: Mapped[str] = mapped_column(String(120), nullable=False)
    # The inventory template the build produced (the copies' source), e.g. srv2022.
    template_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # sha256 (hex) of the build's manifest; stamped on every copy as ``tn-build: <id> sha256:<checksum>``.
    checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    build_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    iso_checksum: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    # JSON {host short name: copy name}, e.g. {"esx03": "srv2022@esx03-20261021"}.
    copies: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    registered_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    promoted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
