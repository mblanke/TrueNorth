"""0fdeb8975b40 adds class attendance and close-out, and only that (ADR 0014 §12-13, issue #283).

Upgrade from its parent onto a database that already holds a class with a roll call and a
session: ``class_attendance``, ``class_results`` and ``class_closeouts`` appear with their keys
and indexes, ``classes`` is unchanged, and no row is written. The ORM then writes an attendance
row, a result and a close-out. Downgrade refuses while a class is closed (the parent knows no
such status), and otherwise drops what the upgrade added; re-upgrade restores it. SQLite here;
the PostgreSQL chain runs in CI (test_migration_populated_upgrade.py and
test_migration_downgrades.py with TEST_POSTGRES_ADMIN_URL).
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
PARENT, REVISION = "57e2c4b65b98", "0fdeb8975b40"
TABLES = {"class_attendance", "class_results", "class_closeouts"}


def _alembic(url: str, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True,
                          timeout=300)
    if ok:
        assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"
    return done


def test_the_revision_chains_onto_the_previous_head():
    text = (API / "alembic/versions/0fdeb8975b40_class_attendance_closeout.py").read_text(encoding="utf-8")
    assert f'down_revision: str | None = "{PARENT}"' in text


@pytest.mark.slow
def test_upgrade_adds_the_attendance_and_closeout_tables_and_downgrade_removes_them(tmp_path):
    url = f"sqlite:///{tmp_path / 'closeout.db'}"
    _alembic(url, "upgrade", PARENT)
    engine = sa.create_engine(url)
    tenant, course, lead, cls, event, entry, link = (uuid.uuid4() for _ in range(7))
    start = datetime.now(UTC) - timedelta(days=1)
    from app.classes.models import Class, ClassSession
    from app.models import Course, Tenant, User, UserRole
    from app.scheduler.models import EventState, ScheduledEvent
    from sqlalchemy.orm import Session

    with Session(engine) as db:  # tables this revision leaves alone: the ORM is the parent's schema
        db.add(Tenant(id=tenant, name="t", slug="t"))
        db.flush()
        db.add(Course(id=course, name="C101", tenant_id=tenant))
        db.add(User(id=lead, keycloak_id="kc", email="l@corp.test", display_name="Lead", role=UserRole.instructor,
                    tenant_id=tenant))
        db.flush()
        db.add(Class(id=cls, tenant_id=tenant, name="C-1", course_id=course, start_date=date.today(),
                     end_date=date.today(), lead_instructor_id=lead, status="open"))
        db.add(ScheduledEvent(id=event, name="Session 1", state=EventState.completed, tenant_id=tenant,
                              start_time=start, end_time=start + timedelta(hours=2)))
        db.flush()
        db.add(ClassSession(id=link, class_id=cls, event_id=event, tenant_id=tenant))
        db.commit()
    with engine.begin() as db:  # SQL, not the ORM: the table is this revision's, older than the model's columns
        db.execute(sa.text("INSERT INTO class_roll_call (id, class_id, tenant_id, email, status) "
                           "VALUES (:i, :c, :t, 'ann@corp.test', 'invited')"),
                   {"i": str(entry), "c": str(cls), "t": str(tenant)})
    classes_before = {c["name"] for c in sa.inspect(engine).get_columns("classes")}
    assert not TABLES & set(sa.inspect(engine).get_table_names())

    _alembic(url, "upgrade", REVISION)
    insp = sa.inspect(engine)
    assert set(insp.get_table_names()) >= TABLES
    assert {c["name"] for c in insp.get_columns("classes")} == classes_before
    assert {tuple(u["column_names"]) for u in insp.get_unique_constraints("class_attendance")} == {
        ("session_id", "entry_id")}
    assert {tuple(u["column_names"]) for u in insp.get_unique_constraints("class_results")} == {
        ("class_id", "entry_id")}
    assert "ix_class_attendance_class" in {i["name"] for i in insp.get_indexes("class_attendance")}
    assert "ix_class_results_user" in {i["name"] for i in insp.get_indexes("class_results")}
    assert {fk["referred_table"] for fk in insp.get_foreign_keys("class_attendance")} == {
        "tenants", "classes", "class_sessions", "class_roll_call", "users"}
    assert insp.get_pk_constraint("class_closeouts")["constrained_columns"] == ["class_id"]
    # No numeric result column anywhere (Unclassified DLP).
    assert not {"score", "grade", "final_score", "final_grade"} & {c["name"] for c in insp.get_columns("class_results")}
    with engine.connect() as db:
        for table in TABLES:
            assert db.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0

    # The ORM writes attendance, a result and a close-out onto the migrated schema.
    from app.classes.models import ClassAttendance, ClassCloseout, ClassResult

    with Session(engine) as db:
        db.add(ClassAttendance(tenant_id=tenant, class_id=cls, session_id=link, entry_id=entry, status="absent",
                               source="auto"))
        db.add(ClassResult(tenant_id=tenant, class_id=cls, entry_id=entry, outcome="incomplete"))
        db.add(ClassCloseout(class_id=cls, tenant_id=tenant, closed_at=datetime.now(UTC), closed_by=lead))
        db.get(Class, cls).status = "closed"
        db.commit()

    # A closed class: the downgrade refuses rather than leave a status the parent does not know.
    refused = _alembic(url, "downgrade", PARENT, ok=False)
    assert refused.returncode != 0 and "Reopen them" in (refused.stdout + refused.stderr)
    assert set(sa.inspect(engine).get_table_names()) >= TABLES

    with engine.begin() as db:
        db.execute(sa.text("UPDATE classes SET status = 'open'"))
    _alembic(url, "downgrade", PARENT)
    insp = sa.inspect(engine)
    assert not TABLES & set(insp.get_table_names())
    with engine.connect() as db:  # the class, its roll call and its session are kept
        assert db.execute(sa.text("SELECT count(*) FROM classes")).scalar() == 1
        assert db.execute(sa.text("SELECT count(*) FROM class_sessions")).scalar() == 1

    _alembic(url, "upgrade", "head")
    assert set(sa.inspect(engine).get_table_names()) >= TABLES
    engine.dispose()
