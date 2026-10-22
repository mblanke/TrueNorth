"""The Range Certification Record tables (ADR 0013 §6, issue #244).

``range_certification_records``: one row per deployment, a provision of a range (its
``range_operations`` row, ``deployment_id``). What was built, from what, and who asked.

``range_certification_entries``: the evidence, one typed section entry per row, numbered
per record (``seq``). A section's state is its latest entry; a correction is a new entry
naming the one it corrects (``corrects_seq``), with an identity and a reason.

Neither table is ever updated or deleted:

* PostgreSQL: triggers refuse UPDATE, DELETE and TRUNCATE (``tn_refuse_change``) and stamp
  every insert with the database clock (migration 68d00729cc34), so nothing is backdated;
* every database (SQLite in tests too): the ORM hooks below refuse a flushed change or
  delete of a loaded row, and ``_refuse_bulk`` refuses a bulk UPDATE or DELETE through a
  Session;
* the one exception is retention (owner decision 2026-10-21, retention.py): an
  **archived** record (not its range's newest) older than RANGE_CERT_RETENTION_MONTHS is
  deleted with its entries, after an audit row naming its signed export's hash. The
  trigger allows that only in a transaction that set ``tn.cert_purge``, only for a record
  at least 30 days old that is not its range's newest, and an entry only once its record
  is gone (the cascade of that delete);
* ``service.py`` has no update or delete function (tests/api/test_range_certification.py
  checks that, and the hooks).

No foreign keys to ranges, templates or users: the record outlives all of them (a purged
range keeps its teardown evidence), and nothing can cascade a delete into it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event, func
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..db import Base
from ..models import GUID

PURGE_OPTION = "range_certification_purge"  # retention.py only
RECORDS = "range_certification_records"
KEYS = "range_certification_signing_keys"
ENTRIES = "range_certification_entries"


def _now() -> datetime:
    return datetime.now(UTC)


class RecordImmutableError(RuntimeError):
    """An attempt to change or delete certification evidence. Corrections append."""


class RangeCertificationRecord(Base):
    __tablename__ = RECORDS
    __table_args__ = (
        UniqueConstraint("deployment_id", name="uq_range_certification_records_deployment"),
        Index("ix_range_certification_records_range", "tenant_id", "range_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    # The provision operation (range_operations.id) this record certifies.
    deployment_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    range_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    range_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    template_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    template_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    template_version: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    # SHA-256 of what the build was planned from, and what that was (service.plan_hash).
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_hash_basis: Mapped[str] = mapped_column(String(64), nullable=False)
    # SHA-256 of the range's provisioner_output when this build was accepted ("" when none):
    # output still equal to it was not written by this build, so it is not its evidence.
    prior_output_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    opened_by_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    opened_by: Mapped[str] = mapped_column(String(255), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )


class RangeCertificationEntry(Base):
    __tablename__ = ENTRIES
    __table_args__ = (
        UniqueConstraint("record_id", "seq", name="uq_range_certification_entries_seq"),
        Index("ix_range_certification_entries_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    # CASCADE: the retention purge deletes a record and the database takes its entries with
    # it; PostgreSQL refuses any entry DELETE whose record still exists.
    record_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey(f"{RECORDS}.id", ondelete="CASCADE"),
                                                 nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # 1, 2, 3 ... per record
    section: Mapped[str] = mapped_column(String(32), nullable=False)  # sections.SECTIONS
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # sections.STATUSES
    summary: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Who wrote it: "api.capture", "worker.s1", "correction", ... (sections.py)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)  # a person's name, or "system"
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrects_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )


class SigningKey(Base):
    """A public key that has signed certification exports (signing.py), kept after its
    TN_SECRETS_KEY is rotated out so earlier exports still verify. Never changed or deleted."""

    __tablename__ = KEYS

    key_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    alg: Mapped[str] = mapped_column(String(16), nullable=False)
    public_key: Mapped[str] = mapped_column(String(128), nullable=False)  # base64 of the raw key
    first_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )


@event.listens_for(RangeCertificationRecord, "before_update")
@event.listens_for(RangeCertificationEntry, "before_update")
@event.listens_for(SigningKey, "before_update")
def _refuse_update(mapper, connection, target) -> None:  # noqa: ARG001
    raise RecordImmutableError(f"{target.__tablename__}: certification evidence is never changed; append a correction")


@event.listens_for(RangeCertificationRecord, "before_delete")
@event.listens_for(RangeCertificationEntry, "before_delete")
@event.listens_for(SigningKey, "before_delete")
def _refuse_delete(mapper, connection, target) -> None:  # noqa: ARG001
    raise RecordImmutableError(f"{target.__tablename__}: certification evidence is never deleted")


@event.listens_for(Session, "do_orm_execute")
def _refuse_bulk(state) -> None:
    """A bulk ``query(...).update()``/``delete()`` or ``session.execute(update(...))`` skips
    the mapper hooks above; refuse it here for these two tables. The one exception is the
    retention purge (retention.py), a bulk DELETE marked ``PURGE_OPTION``; PostgreSQL
    checks it again (the trigger lets an archived record go only in a purge transaction)."""
    if not (state.is_update or state.is_delete):
        return
    table = getattr(state.statement, "table", None)
    if state.is_delete and state.execution_options.get(PURGE_OPTION) and getattr(table, "name", None) != KEYS:
        return
    if getattr(table, "name", None) in (RECORDS, ENTRIES, KEYS):
        raise RecordImmutableError(f"{table.name}: certification evidence is never changed or deleted")
