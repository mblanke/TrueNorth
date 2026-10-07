"""Scenario run evidence: what each inject did, and scenario executions outside an exercise.

The worker writes both tables (control-plane/worker/worker/scenario_db.py mirrors them);
the API only reads them, apart from creating an execution before it is dispatched.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ..models import GUID, TimestampMixin

# fired    the injector ran and returned success
# failed   unknown action, invalid params, injector error, or no range to run it on
# skipped  deliberately not run: needs range hosts the range does not have (mock backend),
#          or the exercise was no longer running when an instructor inject arrived
INJECT_STATUSES = ("fired", "failed", "skipped")

# pending -> running -> completed | failed
EXECUTION_STATES = ("pending", "running", "completed", "failed")


class InjectRecord(TimestampMixin, Base):
    """One inject's outcome: a timeline event of an exercise or execution, or an
    instructor inject. Kept when the run is replayed, so earlier evidence survives."""

    __tablename__ = "inject_records"
    __table_args__ = (
        Index("ix_inject_records_exercise", "exercise_id"),
        Index("ix_inject_records_execution", "execution_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    exercise_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("exercises.id", ondelete="CASCADE"), nullable=True
    )
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("scenario_executions.id", ondelete="CASCADE"), nullable=True
    )
    range_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    # One run of a timeline (the Celery task id, the same across its retries): a retry
    # skips the events its run already recorded; a replay is a new run.
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="timeline")  # timeline | instructor
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)  # timeline index; None for instructor
    t: Mapped[str | None] = mapped_column(String(16), nullable=True)  # timeline offset "m:ss"
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    execution_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)  # simulated | live
    mitre_technique: Mapped[str | None] = mapped_column(String(32), nullable=True)
    telemetry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    telemetry_shipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ScenarioExecution(TimestampMixin, Base):
    """A scenario's timeline run against a range without an exercise (POST /scenarios/execute).

    Objectives are reported, not scored: an execution has no Students and no evidence
    review, so its objectives stay ``unassessed``. Deleting the scenario keeps the
    execution (``scenario_id`` becomes NULL; ``scenario_name`` remains).
    """

    __tablename__ = "scenario_executions"
    __table_args__ = (Index("ix_scenario_executions_tenant", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tenants.id"), nullable=False)
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("scenarios.id", ondelete="SET NULL"), nullable=True
    )
    scenario_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    range_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("ranges.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    definition: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
