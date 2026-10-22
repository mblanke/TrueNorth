"""Retention of archived certification records (owner decision 2026-10-21, issue #244).

A range's **newest** record is its current one and is never purged, whatever its age and
whether or not the range is still live. Every earlier record is **archived**. An archived
record opened more than ``RANGE_CERT_RETENTION_MONTHS`` calendar months ago (default 12;
0 keeps everything forever; installer ``tn_range_cert_retention_months``) is deleted with
its entries by ``purge``, run every ``RANGE_CERT_PURGE_SECONDS`` (default 86400; 0 off)
from the API's lifespan (``loop``).

Before that it can be exported (GET /ranges/{id}/certification/export?deployment_id=...;
the archived list shows each record's ``purge_after``). Each record goes in its own
transaction, which:

1. takes a transaction-scoped advisory lock (PostgreSQL), so two API processes running
   the loop never purge at once;
2. deletes the record; the database deletes its entries with it (ON DELETE CASCADE; the
   entries trigger refuses an entry DELETE while its record exists);
3. only if that delete removed the row, writes one audit row (``range_certification.purge``)
   naming the record, its deployment, how many entries went and its ``record_sha256``: the
   hash every signed export of the record in that state carries, so an export kept
   elsewhere can be matched to the purge.

This module is the only delete path. It marks its statements ``PURGE_OPTION`` (the ORM
guard in models.py lets only those through) and, on PostgreSQL, sets ``tn.cert_purge``
for the transaction: the trigger then still refuses a record that is its range's newest or
younger than 30 days.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from sqlalchemy import delete, exists, select, text
from sqlalchemy.orm import Session, aliased

from ..models import AuditLog, Tenant
from . import service
from .models import PURGE_OPTION, RangeCertificationEntry, RangeCertificationRecord

logger = logging.getLogger("truenorth.range_certification.retention")
BATCH = 200


def retention_months() -> int:
    try:
        return max(0, int(os.getenv("RANGE_CERT_RETENTION_MONTHS", "12")))
    except ValueError:
        return 12


def interval() -> float:
    try:
        return float(os.getenv("RANGE_CERT_PURGE_SECONDS", "86400"))
    except ValueError:
        return 86400.0


def shift_months(when: datetime, months: int) -> datetime:
    """``when`` moved by ``months`` calendar months (negative: back), the day clamped to
    the target month's end."""
    total = when.year * 12 + (when.month - 1) + months
    year, month = divmod(total, 12)
    for day in (when.day, 30, 29, 28):
        try:
            return when.replace(year=year, month=month + 1, day=day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def purge_after(opened_at: datetime, months: int | None = None) -> datetime | None:
    """When an archived record opened at ``opened_at`` becomes purgeable (None: never)."""
    months = retention_months() if months is None else months
    if months <= 0:
        return None
    return shift_months(opened_at if opened_at.tzinfo else opened_at.replace(tzinfo=UTC), months)


def purgeable(db: Session, now: datetime | None = None, months: int | None = None) -> list[RangeCertificationRecord]:
    months = retention_months() if months is None else months
    if months <= 0:
        return []
    cutoff = shift_months(now or datetime.now(UTC), -months)
    newer = aliased(RangeCertificationRecord)
    has_newer = exists().where(newer.tenant_id == RangeCertificationRecord.tenant_id,
                               newer.range_id == RangeCertificationRecord.range_id,
                               newer.generation > RangeCertificationRecord.generation)
    return list(db.scalars(select(RangeCertificationRecord)
                           .where(RangeCertificationRecord.opened_at < cutoff, has_newer)
                           .order_by(RangeCertificationRecord.opened_at).limit(BATCH)))


LOCK_KEY = 0x7243_5072  # pg_advisory_xact_lock key of the purge ("rCPr")


def _purge_one(db: Session, record: RangeCertificationRecord, months: int) -> bool:
    """Delete one archived record (and, by cascade, its entries) with its audit row. Does
    not commit. False: someone else purged it first (nothing written)."""
    doc, record_sha = service.record_document(db, record)
    entries = sum(len(s["entries"]) for s in doc["sections"])
    postgres = db.get_bind().dialect.name == "postgresql"
    if postgres:
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        db.execute(text("SELECT set_config('tn.cert_purge', 'on', true)"))
    opts = {PURGE_OPTION: True, "synchronize_session": False}
    if not postgres:  # no trigger and no guaranteed cascade (SQLite keys may be off): by hand
        db.execute(delete(RangeCertificationEntry).where(RangeCertificationEntry.record_id == record.id)
                   .execution_options(**opts))
    gone = db.execute(delete(RangeCertificationRecord).where(RangeCertificationRecord.id == record.id)
                      .execution_options(**opts)).rowcount
    if not gone:
        return False
    # The record outlives its tenant (no foreign key); the audit row's tenant must exist.
    tenant = record.tenant_id if db.get(Tenant, record.tenant_id) is not None else None
    db.add(AuditLog(user_id=None, tenant_id=tenant, action="range_certification.purge",
                    resource_type="range", resource_id=str(record.range_id),
                    detail=(f"tenant={record.tenant_id}; record={record.id}; deployment={record.deployment_id}; "
                            f"generation={record.generation}; opened_at={record.opened_at.isoformat()}; "
                            f"entries={entries}; retention_months={months}; record_sha256={record_sha}")))
    return True


def purge(db: Session, now: datetime | None = None, months: int | None = None) -> int:
    """Delete every archived record past retention, one transaction (and audit row) each.
    Returns how many went. Stops at the first failure (logged)."""
    months = retention_months() if months is None else months
    done = 0
    for record in purgeable(db, now, months):
        try:
            done += _purge_one(db, record, months)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.error("certification record %s not purged (%s); stopping this round", record.id, exc)
            break
    return done


async def loop() -> None:
    every = interval()
    if every <= 0 or retention_months() <= 0:
        logger.info("Certification record retention is off (RANGE_CERT_PURGE_SECONDS or RANGE_CERT_RETENTION_MONTHS 0)")
        return
    from ..db import SessionLocal

    def _round() -> int:
        with SessionLocal() as db:
            return purge(db)

    while True:
        await asyncio.sleep(every)
        try:
            if n := await asyncio.to_thread(_round):
                logger.info("Purged %d archived certification record(s) past retention", n)
        except Exception:  # noqa: BLE001 — the loop outlives any one round
            logger.exception("Certification record retention round failed")
