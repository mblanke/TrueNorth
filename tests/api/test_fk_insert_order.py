"""Foreign keys and the unit of work's write order.

With no relationship() between two mappers, SQLAlchemy does not know one row depends on the
other: it writes them in the order of their mappers' sort keys ("module.ClassName"), not in
the order they were added. A parent whose key is known before its flush (a natural key, an
explicit id) and a child added in the same flush is then inserted child first whenever the
child's class sorts first, and PostgreSQL refuses the key. SQLite ignores foreign keys unless
asked, which is why ``db_session`` (tests/conftest.py) asks; these tests keep it honest.
"""

from __future__ import annotations

import uuid

import pytest
from app.models import StorageAppliance, Tenant
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def _appliance(tenant_id: uuid.UUID) -> StorageAppliance:
    return StorageAppliance(name="a", vendor="v", model="m", management_ip="192.0.2.1", tenant_id=tenant_id)


def test_db_session_enforces_foreign_keys(db_session):
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), db_session.begin_nested():
        db_session.add(_appliance(uuid.uuid4()))
        db_session.flush()


def test_db_session_does_not_autoflush(db_session):
    """Like app.db.SessionLocal: a query does not write what was added before it."""
    tenant = Tenant(name="t", slug=f"t-{uuid.uuid4().hex[:6]}")
    db_session.add(tenant)
    assert db_session.query(Tenant).filter_by(slug=tenant.slug).first() is None


def _same_flush(session: Session) -> None:
    tenant = Tenant(id=uuid.uuid4(), name="t", slug=f"t-{uuid.uuid4().hex[:6]}")
    session.add(tenant)
    session.add(_appliance(tenant.id))  # StorageAppliance sorts before Tenant
    session.flush()


def _parent_flushed_first(session: Session) -> None:
    tenant = Tenant(id=uuid.uuid4(), name="t", slug=f"t-{uuid.uuid4().hex[:6]}")
    session.add(tenant)
    session.flush()
    session.add(_appliance(tenant.id))
    session.flush()


def test_an_unrelated_parent_and_child_in_one_flush_are_written_child_first(db_session):
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), db_session.begin_nested():
        _same_flush(db_session)


def test_flushing_the_parent_first_is_the_fix(db_session):
    _parent_flushed_first(db_session)


def test_postgresql_refuses_the_same_flush_and_takes_the_fix(postgres_engine):
    with Session(postgres_engine, autoflush=False) as s, pytest.raises(IntegrityError, match="storage_appliances"):
        _same_flush(s)
    with Session(postgres_engine, autoflush=False) as s:
        _parent_flushed_first(s)
        s.commit()
