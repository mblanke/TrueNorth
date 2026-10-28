"""AD status sync with real commits (#309 round 3, item 2).

The ``db_session`` fixture joins an outer transaction that a rollback undoes whole, so it
cannot show what one account's failure does to the rest of a run. These tests use their own
SQLite file and a plain session that commits, as ``SessionLocal`` does in production.

Each account's deactivation runs in its own savepoint, and every result row that is not an
applied deactivation (held, unconfirmed, kept as last admin) is committed at once: a failure
on one account rolls back that account only, never another account's recorded finding (which
tomorrow night's run needs as its confirmation).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import app.main  # noqa: F401  (registers every table)
import pytest
import sqlalchemy as sa
from app.ad_status_sync import service
from app.ad_status_sync.directory import DirectoryReader, DirectorySnapshot, DirectoryUser
from app.ad_status_sync.models import AdStatusSyncResult
from app.db import Base
from app.models import Tenant, User, UserRole
from sqlalchemy.orm import sessionmaker


class Directory(DirectoryReader):
    def __init__(self):
        self.users: dict[str, DirectoryUser] = {}

    def ad(self, u: User, enabled: bool = True) -> None:
        self.users[u.keycloak_id] = DirectoryUser(u.keycloak_id, u.email, u.email, enabled, True)

    def snapshot(self) -> DirectorySnapshot:
        return DirectorySnapshot(users=dict(self.users))


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("AD_STATUS_SYNC_DRY_RUN", "false")
    monkeypatch.setenv("AD_STATUS_SYNC_HOUR_UTC", "0")
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'sync.db'}")
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()
    eng.dispose()


def _accounts(db, n: int, role=UserRole.student):
    t = Tenant(id=uuid.uuid4(), name="t", slug=f"t-{uuid.uuid4().hex[:6]}")
    db.add(t)
    db.flush()
    ids = sorted(uuid.uuid4() for _ in range(n))  # the run walks accounts in id order
    users = [User(id=ids[i], keycloak_id=f"kc-{ids[i].hex[:8]}", email=f"u{i}@corp.test", display_name=f"u{i}",
                  role=role, tenant_id=t.id, source="ad", ad_object_guid=str(uuid.uuid4())) for i in range(n)]
    db.add_all(users)
    db.commit()
    return t, users


def test_a_failed_deactivation_keeps_other_accounts_findings(db, monkeypatch):
    """Night 1 finds x. Night 2 first finds y (sorted before x), then x's deactivation fails.
    y's finding must survive, so night 3 confirms and deactivates y."""
    t, (y, x, *others) = _accounts(db, 6)
    kc = Directory()
    for u in (y, *others):
        kc.ad(u)
    kc.ad(x, enabled=False)
    night = datetime.now(UTC).replace(hour=3) - timedelta(days=3)
    service.run_nightly(db, kc, now=night)  # night 1: x found
    kc.ad(y, enabled=False)
    real = service.deactivate

    def failing(db_, run, u, reason, now):
        if u.id == x.id:
            raise RuntimeError("roll-call failure")
        return real(db_, run, u, reason, now)

    monkeypatch.setattr(service, "deactivate", failing)
    run2 = service.run_nightly(db, kc, now=night + timedelta(days=1)).run  # night 2: y found; x fails
    monkeypatch.setattr(service, "deactivate", real)
    found = {r.user_id for r in db.query(AdStatusSyncResult).filter(AdStatusSyncResult.run_id == run2.id)}
    assert y.id in found, "y's finding on night 2 was rolled back by x's failure"
    assert x.id in found  # the failed one is still a finding: night 3 confirms it and tries again
    assert run2.counts["by_tenant"][str(t.id)]["errors"] == 1
    db.refresh(x)
    assert x.is_active  # its savepoint rolled back: not half-deactivated
    service.run_nightly(db, kc, now=night + timedelta(days=2))  # night 3
    db.refresh(y)
    db.refresh(x)
    assert not y.is_active and not x.is_active  # both confirmed by night 2's findings


def test_kept_last_admin_is_committed_at_once(db, monkeypatch):
    """A last admin found disabled is kept, and that finding (and the transaction holding the
    admin row lock) is committed at once: a later account's failure does not undo it."""
    t, users = _accounts(db, 6)
    admin, x, others = users[0], users[1], users[2:]
    admin.role = UserRole.admin  # the tenant's only admin, sorted first
    db.commit()
    kc = Directory()
    for u in others:
        kc.ad(u)
    kc.ad(admin, enabled=False)
    kc.ad(x, enabled=False)
    night = datetime.now(UTC).replace(hour=3) - timedelta(days=3)
    service.run_nightly(db, kc, now=night)
    real = service.deactivate
    monkeypatch.setattr(service, "deactivate", lambda db_, run, u, reason, now: (_ for _ in ()).throw(
        RuntimeError("roll-call failure")) if u.id == x.id else real(db_, run, u, reason, now))
    run2 = service.run_nightly(db, kc, now=night + timedelta(days=1)).run
    assert run2.counts["by_tenant"][str(t.id)]["kept_last_admin"] == 1
    found = {r.user_id for r in db.query(AdStatusSyncResult).filter(AdStatusSyncResult.run_id == run2.id)}
    assert admin.id in found, "the kept admin's finding was rolled back by x's failure"
    db.refresh(admin)
    assert admin.is_active
