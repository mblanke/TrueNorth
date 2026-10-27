"""57e2c4b65b98 adds class session ranges, and only that (ADR 0014 §9-11, issue #282).

Upgrade from its parent onto a database that already holds a class with a roll call and a
session: ``class_sessions`` gains ``range_mode`` (the old session is collective, which is what
it was), and ``class_course_range_modes``, ``class_ranges`` and ``class_session_teams`` appear
with their keys and indexes; no row is written. The ORM then writes a class range and a session
team. Downgrade refuses while a class range still stands (nothing would tear it down), and
otherwise drops what the upgrade added; re-upgrade restores it. SQLite here; the PostgreSQL
chain runs in CI (test_migration_populated_upgrade.py and test_migration_downgrades.py with
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
PARENT, REVISION = "8bc4d6fa0532", "57e2c4b65b98"
TABLES = {"class_course_range_modes", "class_ranges", "class_session_teams"}


def _alembic(url: str, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True,
                          timeout=300)
    if ok:
        assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"
    return done


def test_the_revision_chains_onto_the_previous_head():
    text = (API / "alembic/versions/57e2c4b65b98_class_session_ranges.py").read_text(encoding="utf-8")
    assert f'down_revision: str | None = "{PARENT}"' in text


@pytest.mark.slow
def test_upgrade_adds_the_class_range_tables_and_downgrade_removes_them(tmp_path):
    url = f"sqlite:///{tmp_path / 'ranges.db'}"
    _alembic(url, "upgrade", PARENT)
    engine = sa.create_engine(url)
    tenant, course, lead, cls, entry, event, link, tmpl, rng = (uuid.uuid4() for _ in range(9))
    start = datetime.now(UTC) + timedelta(days=3)
    from app.classes.models import Class
    from app.models import Course, Range, RangeState, Template, Tenant, User, UserRole
    from app.scheduler.models import EventState, ScheduledEvent
    from sqlalchemy.orm import Session

    with Session(engine) as db:  # tables this revision leaves alone: their ORM is the parent's schema
        db.add(Tenant(id=tenant, name="t", slug="t"))
        db.flush()
        db.add(Course(id=course, name="C101", tenant_id=tenant))
        db.add(User(id=lead, keycloak_id="kc", email="l@corp.test", display_name="Lead", role=UserRole.instructor,
                    tenant_id=tenant))
        db.add(Template(id=tmpl, name="seat", yaml="nodes: [{id: ws, os: kali}]\n", tenant_id=tenant))
        db.flush()
        db.add(Class(id=cls, tenant_id=tenant, name="C-1", course_id=course, start_date=date.today(),
                     end_date=date.today(), lead_instructor_id=lead, status="open"))
        db.add(ScheduledEvent(id=event, name="Session 1", state=EventState.scheduled, tenant_id=tenant,
                              start_time=start, end_time=start + timedelta(hours=2), template_id=tmpl, vcpu_total=4))
        db.add(Range(id=rng, name="seat", template_id=tmpl, tenant_id=tenant, state=RangeState.created))
        db.commit()
    with engine.begin() as db:  # class_sessions gains a column: written in SQL, as the parent had it
        db.execute(sa.text("INSERT INTO class_roll_call (id, class_id, tenant_id, email, status) "
                           "VALUES (:i, :c, :t, 'ann@corp.test', 'invited')"),
                   {"i": entry.hex, "c": cls.hex, "t": tenant.hex})
        db.execute(sa.text("INSERT INTO class_sessions (id, class_id, event_id, tenant_id) VALUES (:i, :c, :e, :t)"),
                   {"i": link.hex, "c": cls.hex, "e": event.hex, "t": tenant.hex})
    assert not TABLES & set(sa.inspect(engine).get_table_names())

    _alembic(url, "upgrade", REVISION)
    insp = sa.inspect(engine)
    assert set(insp.get_table_names()) >= TABLES
    assert {c["name"] for c in insp.get_columns("class_ranges")} == {
        "id", "tenant_id", "class_id", "template_id", "kind", "entry_id", "seat_key", "range_id", "builds",
        "built_at", "torn_down_at", "teardown_reason", "last_error", "created_at", "updated_at"}
    assert {tuple(u["column_names"]) for u in insp.get_unique_constraints("class_ranges")} == {
        ("class_id", "template_id", "seat_key")}
    assert {"ix_class_ranges_range", "ix_class_ranges_tenant"} <= {i["name"] for i in insp.get_indexes("class_ranges")}
    assert {fk["referred_table"] for fk in insp.get_foreign_keys("class_ranges")} == {
        "tenants", "classes", "templates", "class_roll_call", "ranges"}
    assert insp.get_pk_constraint("class_course_range_modes")["constrained_columns"] == ["tenant_id", "course_id"]
    assert {tuple(u["column_names"]) for u in insp.get_unique_constraints("class_session_teams")} == {("team_id",)}
    with engine.connect() as db:  # the old session is collective; nothing written
        assert db.execute(sa.text("SELECT range_mode FROM class_sessions")).scalar() == "collective"
        for table in TABLES:
            assert db.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0

    # The ORM writes a seat range and a session team onto the migrated schema.
    from app.classes.models import ClassRange, ClassSessionTeam
    from app.models import Team

    team = uuid.uuid4()
    with Session(engine) as db:
        db.add(ClassRange(tenant_id=tenant, class_id=cls, template_id=tmpl, kind="seat", entry_id=entry,
                          seat_key=str(entry), range_id=rng, builds=1))
        db.add(Team(id=team, name="Team 1", tenant_id=tenant))
        db.flush()
        db.add(ClassSessionTeam(tenant_id=tenant, session_id=link, team_id=team, position=0))
        db.commit()

    # A class range still standing: the downgrade refuses rather than orphan it.
    refused = _alembic(url, "downgrade", PARENT, ok=False)
    assert refused.returncode != 0 and "still stand" in (refused.stdout + refused.stderr)
    assert set(sa.inspect(engine).get_table_names()) >= TABLES

    with engine.begin() as db:
        db.execute(sa.text("UPDATE class_ranges SET torn_down_at = CURRENT_TIMESTAMP"))
    _alembic(url, "downgrade", PARENT)
    insp = sa.inspect(engine)
    assert not TABLES & set(insp.get_table_names())
    assert "range_mode" not in {c["name"] for c in insp.get_columns("class_sessions")}
    with engine.connect() as db:  # the session, the range and the team are kept
        assert db.execute(sa.text("SELECT count(*) FROM class_sessions")).scalar() == 1
        assert db.execute(sa.text("SELECT count(*) FROM teams")).scalar() == 1

    _alembic(url, "upgrade", "head")
    assert set(sa.inspect(engine).get_table_names()) >= TABLES
    engine.dispose()
