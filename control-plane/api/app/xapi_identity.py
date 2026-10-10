"""The xAPI identity switch: who each pre-switch LRS statement was about.

Until migration ``5a1e9c3d7b20`` TrueNorth identified people in xAPI by email
(``actor.mbox = mailto:<email>``). It now uses an ``account`` IFI whose name is
``users.id`` (``app/xapi.py``). Statements already in the LRS are immutable and stay as
they were (docs/xapi-conformance.md, "Statements already in the LRS"); this table keeps
the link between the two identities, frozen at the time of the switch, so that history
stays attributable after an email changes, and so ``python -m app.xapi_reissue`` can
re-issue it under the new identity when an operator decides to.

Kept out of ``app/models.py`` (ADR 0003: per-section modules).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import GUID


class XapiLegacyIdentity(Base):
    """One row per user who existed when the actor changed: the IFI their old statements carry."""

    __tablename__ = "xapi_legacy_identities"
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    legacy_mbox: Mapped[str] = mapped_column(Text, nullable=False)  # "mailto:<email at the switch>"
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
