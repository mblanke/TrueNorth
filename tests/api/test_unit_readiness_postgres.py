"""The unit readiness queries on PostgreSQL (ADR 0014 decision 7, issue #289).

SQLite accepts SQL that PostgreSQL refuses (DISTINCT over joined columns, enum comparisons,
TRIM/LOWER on the unit filter). These run only with TEST_POSTGRES_ADMIN_URL (CI's
test-python job; ``postgres_engine`` is a fresh copy of ``alembic upgrade head``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from app.classes import readiness
from app.classes.models import Class, ClassAttendance, ClassCloseout, ClassResult, ClassSession, RollCallEntry
from app.models import Course, Enrollment, EnrollmentStatus, Qualification, Tenant, User, UserRole
from app.scheduler.models import EventState, ScheduledEvent
from sqlalchemy.orm import Session

pytestmark = pytest.mark.slow

NOW = datetime.now(UTC).replace(microsecond=0)


def test_readiness_on_postgres(postgres_engine):
    tenant, other, course, qual, lead, cls, event, link = (uuid.uuid4() for _ in range(8))
    with Session(postgres_engine) as db:
        db.add_all([Tenant(id=tenant, name="T", slug=f"t-{tenant.hex[:6]}"),
                    Tenant(id=other, name="O", slug=f"o-{other.hex[:6]}")])
        db.add(Qualification(id=qual, qsp_code=f"Q{qual.hex[:6]}", nqual="ALJQ", title="Junior"))
        db.flush()
        db.add(Course(id=course, name="SOC", tenant_id=tenant, qualification_id=qual))
        db.add(User(id=lead, email="lead@corp.test", display_name="Lead", role=UserRole.instructor,
                    tenant_id=tenant, keycloak_id=f"kc-{lead.hex[:8]}"))
        ann, bob = uuid.uuid4(), uuid.uuid4()
        db.add(User(id=ann, email="ann@corp.test", display_name="Ann", role=UserRole.student, tenant_id=tenant,
                    keycloak_id=f"kc-{ann.hex[:8]}", unit="2 Sig Regt"))
        db.add(User(id=bob, email="bob@corp.test", display_name="Bob", role=UserRole.student, tenant_id=tenant,
                    keycloak_id=f"kc-{bob.hex[:8]}", unit="Kingston Det"))
        db.flush()
        db.add(Class(id=cls, tenant_id=tenant, name="C-1", course_id=course, start_date=date.today() - timedelta(days=5),
                     end_date=date.today() - timedelta(days=3), lead_instructor_id=lead, status="closed"))
        db.add(ScheduledEvent(id=event, name="Lab", state=EventState.completed, tenant_id=tenant, instructor_id=lead,
                              start_time=NOW - timedelta(days=5), end_time=NOW - timedelta(days=5, hours=-3)))
        db.flush()
        db.add(ClassSession(id=link, class_id=cls, event_id=event, tenant_id=tenant, created_by=lead))
        db.add(ClassCloseout(class_id=cls, tenant_id=tenant, closed_at=NOW - timedelta(days=2), closed_by=lead))
        entries = {}
        for uid in (ann, bob):
            e = RollCallEntry(id=uuid.uuid4(), class_id=cls, tenant_id=tenant,
                              email="ann@corp.test" if uid == ann else "bob@corp.test", user_id=uid, status="signed_in")
            db.add(e)
            entries[uid] = e
            db.add(Enrollment(user_id=uid, course_id=course, tenant_id=tenant,
                              status=EnrollmentStatus.completed if uid == ann else EnrollmentStatus.failed))
        db.flush()
        for uid, outcome in ((ann, "pass"), (bob, "fail")):
            db.add(ClassAttendance(tenant_id=tenant, class_id=cls, session_id=link, entry_id=entries[uid].id,
                                   user_id=uid, status="present", source="auto"))
            db.add(ClassResult(tenant_id=tenant, class_id=cls, entry_id=entries[uid].id, user_id=uid,
                               outcome=outcome, comment="ok", decided_at=NOW))
        db.commit()

        everyone = readiness.build(db, tenant, date.today() - timedelta(days=30), date.today(), None)
        assert everyone.units == ["2 Sig Regt", "Kingston Det"]
        rows = {s.email: s for s in everyone.classes[0].students}
        assert rows["ann@corp.test"].qualification_earned == "ALJQ"
        assert rows["ann@corp.test"].enrolment_status == "completed"
        assert rows["bob@corp.test"].enrolment_status == "failed"
        assert everyone.summary.present == 2 and everyone.summary.qualifications_earned == 1
        assert everyone.classes[0].sessions_held == 1

        one = readiness.build(db, tenant, date.today() - timedelta(days=30), date.today(), "  2 SIG regt ")
        assert [s.email for c in one.classes for s in c.students] == ["ann@corp.test"]

        assert readiness.build(db, other, date.today() - timedelta(days=30), date.today(), None).classes == []
