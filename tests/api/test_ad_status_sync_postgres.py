"""The last-admin guard under concurrency, on PostgreSQL (#290 round 2, should-fix 3).

SQLite serialises writers, so a race there proves nothing. Runs only with
TEST_POSTGRES_ADMIN_URL (CI's test-python job; ``postgres_engine`` in tests/conftest.py).

A tenant has two active admins. Transaction A checks admin 1 (not the last: admin 2 is
active), deactivates it, and holds its transaction open. Transaction B, at the same moment,
checks admin 2. Without a lock B reads admin 1 as still active (A has not committed) and
would deactivate admin 2 too, leaving none. ``is_last_admin`` locks the tenant's active admin
rows (FOR UPDATE), so B waits for A, then counts again and sees admin 2 is the last.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from app.ad_status_sync.service import is_last_admin
from app.models import Tenant, User, UserRole
from sqlalchemy.orm import Session

pytestmark = pytest.mark.slow

HOLD = 1.5  # seconds A keeps its transaction open after deactivating admin 1


def test_two_admins_deactivated_at_once_leave_one(postgres_engine):
    eng = postgres_engine
    tenant, a1, a2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with Session(eng) as db:
        db.add(Tenant(id=tenant, name="T", slug=f"t-{tenant.hex[:6]}"))
        db.flush()
        for uid in (a1, a2):
            db.add(User(id=uid, email=f"{uid.hex[:8]}@corp.test", display_name="Admin", role=UserRole.admin,
                        tenant_id=tenant, keycloak_id=f"kc-{uid.hex[:8]}"))
        db.commit()

    b_saw_last: list[bool] = []
    a_locked = threading.Event()

    def transaction_b():
        a_locked.wait(10)
        with Session(eng) as db:
            started = time.monotonic()
            b_saw_last.append(is_last_admin(db, db.get(User, a2)))
            b_saw_last.append(time.monotonic() - started >= HOLD / 2)  # it waited for A
            db.rollback()

    with Session(eng) as db:
        u1 = db.get(User, a1)
        assert is_last_admin(db, u1) is False  # admin 2 is active
        db.query(User).filter(User.id == a1).update({User.is_active: False}, synchronize_session=False)
        b = threading.Thread(target=transaction_b)
        b.start()
        a_locked.set()
        time.sleep(HOLD)
        db.commit()
    b.join(30)
    assert b_saw_last == [True, True]
