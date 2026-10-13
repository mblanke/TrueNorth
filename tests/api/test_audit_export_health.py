"""Deep health's ``audit_export`` component (ADR 0011): degraded when the signed export
falls behind while there is something to export."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from app.audit_integrity import AuditExport
from app.health import HealthStatus, audit_export_status
from app.models import AuditLog


def _row(db, days_ago: int) -> None:
    db.add(AuditLog(id=uuid.uuid4(), timestamp=datetime.now(UTC) - timedelta(days=days_ago), action="probe",
                    resource_type="probe", resource_id="x"))
    db.flush()


def _export(db, days_ago: int) -> None:
    h = hashlib.sha256(str(days_ago).encode()).hexdigest()
    db.add(AuditExport(day=(datetime.now(UTC) - timedelta(days=days_ago)).date(), seq=0, row_count=1,
                       file_name="f", file_sha256=h, manifest_sha256=h, key_id="0123456789abcdef"))
    db.flush()


def test_nothing_to_export_is_healthy(db_session):
    assert audit_export_status(db_session)[0] == HealthStatus.HEALTHY
    _row(db_session, 0)
    assert audit_export_status(db_session)[0] == HealthStatus.HEALTHY  # today's rows wait for tomorrow


def test_old_rows_without_an_export_are_degraded(db_session):
    _row(db_session, 5)
    status, message, _ = audit_export_status(db_session)
    assert status == HealthStatus.DEGRADED and "behind" in message


def test_a_stale_export_is_degraded_and_a_current_one_healthy(db_session):
    _row(db_session, 10)
    _export(db_session, 6)
    assert audit_export_status(db_session)[0] == HealthStatus.DEGRADED
    _export(db_session, 1)
    status, _, details = audit_export_status(db_session)
    assert status == HealthStatus.HEALTHY and details["latest_export_day"]
