"""Deep health's ``audit_export`` component (ADR 0011): degraded when the signed export
falls behind while there is something to export."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from app.audit_integrity import AuditExport
from app.health import FIRST_EXPORT_GRACE_S, HealthStatus, audit_export_status
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


# Staging, v1.2.0-rc7: just upgraded, rows from before the upgrade, beat's first export not due
# until 00:45 UTC. Pending, not degraded, for a bounded time after the API starts.


def test_never_exported_is_pending_right_after_a_start(db_session):
    _row(db_session, 5)
    status, message, details = audit_export_status(db_session, uptime_s=600)
    assert status == HealthStatus.HEALTHY and "first export pending" in message
    assert details == {"latest_export_day": None, "first_export_pending": True}


def test_the_grace_is_bounded(db_session):
    _row(db_session, 5)
    status, message, _ = audit_export_status(db_session, uptime_s=FIRST_EXPORT_GRACE_S)
    assert status == HealthStatus.DEGRADED and "behind" in message
    # Beat's first run is at most 24 h 45 min after a start: the grace covers it, and not much more.
    assert 24.75 * 3600 < FIRST_EXPORT_GRACE_S <= 27 * 3600


def test_the_grace_never_covers_a_stale_export(db_session):
    """Once one day was exported, falling behind is degraded at once, whatever the uptime."""
    _row(db_session, 10)
    _export(db_session, 6)
    assert audit_export_status(db_session, uptime_s=60)[0] == HealthStatus.DEGRADED


def test_deep_health_passes_the_api_uptime(monkeypatch):
    import asyncio

    from app import health

    seen = {}

    def fake(db, uptime_s=None):
        seen["uptime_s"] = uptime_s
        return HealthStatus.HEALTHY, "ok", {}

    monkeypatch.setattr(health, "audit_export_status", fake)
    component = asyncio.run(health.HealthChecker()._check_audit_export())
    assert component.status == HealthStatus.HEALTHY and 0 <= seen["uptime_s"] < 60
