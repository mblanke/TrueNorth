"""5e6d8a033133 adds class booking approval, and only that (ADR 0014 §14-17, issue #281).

Upgrade from its parent onto a database that already holds a class with a roll call and a
booking: ``class_sessions`` and ``scheduler_booking_approvals`` appear with their keys and
indexes, ``class_roll_call`` gains ``pending_reason``, the old rows are untouched, and a
pending class session can be written through the ORM. Downgrade refuses while a booking is
pending approval or rejected (the older code cannot read those states), and otherwise drops
what the upgrade added; re-upgrade restores it. SQLite here; the PostgreSQL chain runs in CI
(test_migration_populated_upgrade.py and test_migration_downgrades.py with
TEST_POSTGRES_ADMIN_URL).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"
PARENT, REVISION = "c1465c4964a6", "5e6d8a033133"
TABLES = {"class_sessions", "scheduler_booking_approvals"}


def _alembic(url: str, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True,
                          timeout=300)
    if ok:
        assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"
    return done


def test_the_revision_chains_onto_the_previous_head():
    text = (API / "alembic/versions/5e6d8a033133_class_booking_approval.py").read_text(encoding="utf-8")
    assert f'down_revision: str | None = "{PARENT}"' in text


@pytest.mark.slow
def test_upgrade_adds_the_approval_tables_and_downgrade_removes_them(tmp_path):
    url = f"sqlite:///{tmp_path / 'approval.db'}"
    _alembic(url, "upgrade", PARENT)
    engine = sa.create_engine(url)
    tenant, course, lead, cls, entry, event = (uuid.uuid4() for _ in range(6))
    start = datetime.now(UTC) + timedelta(days=3)
    from app.classes.models import Class
    from app.models import Course, Tenant, User, UserRole
    from app.scheduler.models import EventState, ScheduledEvent
    from sqlalchemy.orm import Session

    with Session(engine) as db:  # tables this revision leaves alone: their ORM is the parent's schema
        db.add(Tenant(id=tenant, name="t", slug="t"))
        db.flush()
        db.add(Course(id=course, name="C101", tenant_id=tenant))
        db.add(User(id=lead, keycloak_id="kc", email="l@corp.test", display_name="Lead", role=UserRole.instructor,
                    tenant_id=tenant))
        db.flush()
        db.add(Class(id=cls, tenant_id=tenant, name="C-1", course_id=course, start_date=date.today(),
                     end_date=date.today(), lead_instructor_id=lead, status="open"))
        db.add(ScheduledEvent(id=event, name="old", state=EventState.scheduled, tenant_id=tenant, start_time=start,
                              end_time=start + timedelta(hours=2), vcpu_total=4))
        db.commit()
    with engine.begin() as db:  # class_roll_call gains a column: written in SQL, as the parent had it
        db.execute(sa.text("INSERT INTO class_roll_call (id, class_id, tenant_id, email, status) "
                           "VALUES (:i, :c, :t, 'ann@corp.test', 'invited')"),
                   {"i": entry.hex, "c": cls.hex, "t": tenant.hex})
    assert not TABLES & set(sa.inspect(engine).get_table_names())

    _alembic(url, "upgrade", REVISION)
    insp = sa.inspect(engine)
    assert set(insp.get_table_names()) >= TABLES
    assert {c["name"] for c in insp.get_columns("class_sessions")} == {
        "id", "class_id", "event_id", "tenant_id", "created_by", "created_at", "updated_at"}
    assert {tuple(u["column_names"]) for u in insp.get_unique_constraints("class_sessions")} == {("event_id",)}
    assert "ix_class_sessions_class" in {i["name"] for i in insp.get_indexes("class_sessions")}
    assert {fk["referred_table"] for fk in insp.get_foreign_keys("class_sessions")} == {
        "classes", "scheduled_events", "tenants", "users"}
    assert {c["name"] for c in insp.get_columns("scheduler_booking_approvals")} == {
        "event_id", "tenant_id", "requested_by", "requested_at", "proposed_start", "proposed_end", "proposed_by",
        "proposed_at", "decided_by", "decided_at", "reason", "created_at", "updated_at"}
    assert insp.get_pk_constraint("scheduler_booking_approvals")["constrained_columns"] == ["event_id"]
    assert "pending_reason" in {c["name"] for c in insp.get_columns("class_roll_call")}
    with engine.connect() as db:  # the old rows are untouched
        assert db.execute(sa.text("SELECT status, pending_reason FROM class_roll_call")).one() == ("invited", None)
        assert db.execute(sa.text("SELECT state FROM scheduled_events")).scalar() == "scheduled"

    # The ORM writes a pending class session onto the migrated schema.
    from app.classes.models import ClassSession
    from app.scheduler.models import BookingApproval

    pending = uuid.uuid4()
    with Session(engine) as db:
        db.add(ScheduledEvent(id=pending, name="Session 1", state=EventState.pending_approval, tenant_id=tenant,
                              start_time=start, end_time=start + timedelta(hours=3), course_id=course))
        db.flush()
        db.add(ClassSession(class_id=cls, event_id=pending, tenant_id=tenant, created_by=lead))
        db.add(BookingApproval(event_id=pending, tenant_id=tenant, requested_by=lead, requested_at=datetime.now(UTC)))
        db.commit()

    # A booking awaiting approval: the downgrade refuses rather than strand it.
    refused = _alembic(url, "downgrade", PARENT, ok=False)
    assert refused.returncode != 0 and "pending approval or rejected" in (refused.stdout + refused.stderr)
    assert set(sa.inspect(engine).get_table_names()) >= TABLES

    with engine.begin() as db:
        db.execute(sa.text("UPDATE scheduled_events SET state = 'cancelled' WHERE state = 'pending_approval'"))
        db.execute(sa.text("UPDATE class_roll_call SET status = 'pending'"))
    _alembic(url, "downgrade", PARENT)
    insp = sa.inspect(engine)
    assert not TABLES & set(insp.get_table_names())
    assert "pending_reason" not in {c["name"] for c in insp.get_columns("class_roll_call")}
    with engine.connect() as db:
        # An addition nobody approved is not left looking like a roll-call entry.
        assert db.execute(sa.text("SELECT status FROM class_roll_call")).scalar() == "removed"
        assert db.execute(sa.text("SELECT count(*) FROM scheduled_events")).scalar() == 2

    _alembic(url, "upgrade", "head")
    assert set(sa.inspect(engine).get_table_names()) >= TABLES
    engine.dispose()
