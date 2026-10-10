"""Which Moodle platforms are TrueNorth's own farm nodes (app/moodle_farm/service.py).

Kept out of ``app/models.py`` (ADR 0003: per-section modules).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID


class ManagedMoodleNode(Base):
    """A platform registration the installer made for a Moodle TrueNorth runs itself
    (``python -m app.moodle_backends.install_cli register|manage``, run in the api
    container). Never set through the API: being managed lets the platform's LTI launches
    bind farm accounts by their locked TrueNorth id, and its server-side traffic reach a
    private address."""

    __tablename__ = "managed_moodle_nodes"

    platform_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("external_platforms.id"), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    node: Mapped[str] = mapped_column(String(64), nullable=False)
    marked_by: Mapped[str] = mapped_column(String(32), nullable=False)  # install_cli register | manage
    marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
