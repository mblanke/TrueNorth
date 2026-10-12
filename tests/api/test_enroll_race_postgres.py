"""Two concurrent self-enrolments on PostgreSQL make one row, and both callers get it.

``POST /courses/{id}/enroll`` checks for an existing enrolment, then inserts. Two requests
can both pass the check; ``uq_user_course`` then refuses the second insert, and the handler
answers with the first request's row (200) instead of a 500. SQLite serialises writers, so
only PostgreSQL shows the race; the test forces both requests past the check before either
inserts. Skips without TEST_POSTGRES_ADMIN_URL (CI sets it).
"""

from __future__ import annotations

import threading
import uuid

from app.auth import CurrentUser
from app.models import Course, Enrollment, Tenant, User, UserRole
from app.routers import courses
from app.schemas import EnrollmentIn
from fastapi import Response
from sqlalchemy.orm import Session


def test_on_postgres_concurrent_self_enrolments_make_one_row(postgres_engine, monkeypatch):
    tid, uid, cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with Session(postgres_engine) as s:
        s.add(Tenant(id=tid, name="race", slug=f"race-{tid.hex[:6]}"))
        s.flush()
        s.add(User(id=uid, email=f"{uid.hex[:8]}@x.test", display_name="s", role=UserRole.student, tenant_id=tid,
                   keycloak_id=f"kc-{uid.hex[:8]}"))
        s.add(Course(id=cid, name="Shared", tenant_id=None, is_published=True))
        s.commit()
    me = CurrentUser(id=str(uid), email="s@x.test", display_name="s", role=UserRole.student, tenant_id=str(tid),
                     keycloak_id=f"kc-{uid.hex[:8]}")

    # Both requests see "not enrolled yet" before either inserts.
    barrier = threading.Barrier(2)
    real_existing = courses._existing_enrollment
    first_look = threading.local()

    def existing_then_wait(db, user_id, course_id):
        row = real_existing(db, user_id, course_id)
        if not getattr(first_look, "done", False):
            first_look.done = True
            barrier.wait(10)
        return row

    monkeypatch.setattr(courses, "_existing_enrollment", existing_then_wait)
    results: list[tuple[int, uuid.UUID]] = []
    errors: list[BaseException] = []

    def enrol():
        try:
            with Session(postgres_engine) as db:
                response = Response()
                response.status_code = 201
                row = courses.enroll_user(cid, EnrollmentIn(user_id=uid, course_id=cid), response, db, me)
                results.append((response.status_code, row.id))
        except BaseException as exc:  # noqa: BLE001 — reported below
            errors.append(exc)

    threads = [threading.Thread(target=enrol) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert not errors, errors
    assert sorted(code for code, _ in results) == [200, 201]
    assert len({row_id for _, row_id in results}) == 1
    with Session(postgres_engine) as s:
        assert s.query(Enrollment).filter(Enrollment.user_id == uid, Enrollment.course_id == cid).count() == 1
