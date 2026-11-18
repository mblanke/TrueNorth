"""Round-3 coverage for app/db.py (whole-slice rank: 53.8% before this file).

The session/health layer underneath every router was only touched incidentally by the
slice's tests. These tests drive the real dependency generators and health check: a
request session is yielded usable and closed, the soft-delete filter separates live from
deleted rows and leaves soft-delete-less models alone, health reports True on the live
engine and False on a dead one, and the read-replica dependency falls back to the
primary when no replica is configured and routes to one when it is. The PostgreSQL-only
statement-timeout connect listeners are unreachable under the suite's SQLite engines and
are not claimed here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app import db as app_db
from app.models import Tenant, User

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")  # the AUTH_DISABLED principal's tenant


# -- the request-session dependency -------------------------------------------------------------


def test_get_db_yields_a_usable_session_and_closes_it_afterwards():
    gen = app_db.get_db()
    session = next(gen)

    assert session.execute(app_db.text("SELECT 1")).scalar() == 1
    assert session.in_transaction()

    gen.close()
    assert not session.in_transaction()  # close released the session's transaction


# -- the soft-delete filter ---------------------------------------------------------------------


def test_not_deleted_hides_soft_deleted_rows(db_session):
    live = User(email="live@covg.test", keycloak_id="covg-live", display_name="Live", tenant_id=DEV_TENANT)
    gone = User(
        email="gone@covg.test",
        keycloak_id="covg-gone",
        display_name="Gone",
        tenant_id=DEV_TENANT,
        deleted_at=datetime.now(UTC),
    )
    db_session.add_all([live, gone])
    db_session.flush()

    rows = app_db.not_deleted(db_session.query(User), User).filter(User.email.like("%@covg.test")).all()

    assert [u.email for u in rows] == ["live@covg.test"]


def test_not_deleted_passes_models_without_soft_delete_through_untouched(db_session):
    query = db_session.query(Tenant)

    assert app_db.not_deleted(query, Tenant) is query


# -- the health check ---------------------------------------------------------------------------


def test_check_db_health_reports_a_working_engine():
    assert app_db.check_db_health() is True


def test_check_db_health_reports_a_dead_engine(monkeypatch):
    broken = app_db.create_engine("sqlite:////nonexistent-covg-dir/nope.db")
    monkeypatch.setattr(app_db, "engine", broken)

    assert app_db.check_db_health() is False


# -- the read-replica dependency ----------------------------------------------------------------


def test_get_read_db_falls_back_to_the_primary_without_a_replica():
    gen = app_db.get_read_db()
    session = next(gen)

    assert session.get_bind() is app_db.engine
    session.execute(app_db.text("SELECT 1"))

    gen.close()
    assert not session.in_transaction()  # close released the session's transaction


def test_get_read_db_routes_to_the_replica_when_one_is_configured(monkeypatch):
    replica_engine = app_db.create_engine("sqlite://")
    monkeypatch.setattr(app_db, "_ReadSession", app_db.sessionmaker(bind=replica_engine, autoflush=False))

    gen = app_db.get_read_db()
    session = next(gen)

    assert session.get_bind() is replica_engine
    session.execute(app_db.text("SELECT 1"))

    gen.close()
    assert not session.in_transaction()  # close released the session's transaction
