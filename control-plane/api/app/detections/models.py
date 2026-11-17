"""Every detection a Student submits to a team exercise, kept as evidence (ADR 0005, 0020).

Rows are written only by the API, from the authenticated request plus the server's own
run of the query: the Student supplies the query and nothing else. Since ADR 0020 a
submission is evidence for the instructor, never a grade: it is ``recorded`` with what it
matched, and becomes ``cited`` when an instructor's acknowledgement of the objective relies
on it. Rows from before ADR 0020 keep their old verdicts as history.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID

PENDING = "pending"  # reserved while its query runs
RECORDED = "recorded"  # the query ran; what it matched is kept for the instructor
CITED = "cited"  # an instructor's acknowledgement of the objective relies on it
SUBMISSIONS = (PENDING, RECORDED, CITED)  # what counts toward a Student's submission bound

# Before ADR 0020 (automatic credit against an answer key): history only, never written now.
ACHIEVED = "achieved"
MISSED = "missed"
UNSCORED = "unscored"
INVALID = "invalid"
CLOSED = "closed"
VERDICTS = (PENDING, RECORDED, CITED, ACHIEVED, MISSED, UNSCORED, INVALID, CLOSED)


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
    events_matched: Mapped[int | None] = mapped_column(Integer, nullable=True)  # what the Student's query matched
    # Before ADR 0020 only (answer-key judging); empty on new rows.
    on_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_precision: Mapped[float | None] = mapped_column(Float, nullable=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    matched_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of matched event ids
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
