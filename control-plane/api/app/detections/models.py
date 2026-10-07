"""Every detection a Student submits, kept whether it found the attack or not (ADR 0005).

Rows are written only by the API, from the authenticated request plus the server's own
evaluation of it: the Student supplies the query and nothing else. An achieved
objective's evidence points here, so a grade can always be traced to what the Student did.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID

PENDING = "pending"  # attempt reserved, being judged
ACHIEVED = "achieved"  # found enough of the attack, precisely enough, and credited
MISSED = "missed"  # judged, and it did not
UNSCORED = "unscored"  # the event store could not answer; not counted as an attempt
INVALID = "invalid"  # the query does not parse; not counted as an attempt
CLOSED = "closed"  # the exercise closed while it was being judged; nothing credited
ATTEMPTS = (PENDING, ACHIEVED, MISSED, CLOSED)  # what uses one of the Student's attempts
VERDICTS = (PENDING, ACHIEVED, MISSED, UNSCORED, INVALID, CLOSED)


class DetectionSubmission(Base):
    __tablename__ = "detection_submissions"
    __table_args__ = (
        Index("ix_detection_submissions_attempts", "exercise_id", "objective_ref", "user_id"),
        Index("ix_detection_submissions_tenant", "tenant_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=True)
    exercise_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("exercises.id"), nullable=False)
    objective_ref: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    events_matched: Mapped[int] = mapped_column(Integer, default=0)  # what the Student's query matched
    on_target: Mapped[int] = mapped_column(Integer, default=0)  # of those, events of the attack
    threshold: Mapped[int] = mapped_column(Integer, default=1)
    min_precision: Mapped[float] = mapped_column(Float, default=0.5)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    matched_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of on-target event ids
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
