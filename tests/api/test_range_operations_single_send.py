"""One pending operation is sent once, however many API processes try at the same time.

Independent review of the candidate (2026-10-05): ``redispatch_pending`` locked the pending
rows with SKIP LOCKED but committed after each send, releasing every other row's lock, and
``dispatch`` sent before its conditional update. The loop runs in every API process, so a
second process could send the same operation again while the first was sending: two tasks
for one operation (and, before range leases, two builds). Now ``dispatch`` holds the
operation's row lock for exactly its own send, and skips a row someone else is sending.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest


def test_on_postgres_two_processes_dispatching_one_operation_send_it_once(monkeypatch):
    import sqlalchemy as sa
    from app import celery_client, range_ops
    from app.models import Range, RangeState, Template, Tenant
    from app.models_range_ops import RangeOperation
    from app.sections import Base
    from sqlalchemy.orm import sessionmaker

    admin_url = os.getenv("TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        pytest.skip("set TEST_POSTGRES_ADMIN_URL to run against Postgres")
    name = f"tn_send_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    engine = sa.create_engine(sa.engine.make_url(admin_url).set(database=name), pool_size=10)
    sends: list[str] = []

    def slow_send(task, *args):  # a broker that takes a moment to answer
        sends.append(task)
        time.sleep(1.0)
        return f"task-{len(sends)}"

    monkeypatch.setattr(celery_client, "dispatch", slow_send)
    try:
        Base.metadata.create_all(engine)
        make = sessionmaker(engine, expire_on_commit=False)
        with make() as s:
            tenant = uuid.uuid4()
            s.add(Tenant(id=tenant, name="t", slug=f"t-{tenant.hex[:8]}"))
            s.flush()
            tpl = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=tenant)
            s.add(tpl)
            s.flush()
            rng = Range(id=uuid.uuid4(), name="r", template_id=tpl.id, tenant_id=tenant, state=RangeState.provisioning)
            s.add(rng)
            s.flush()
            op = RangeOperation(
                id=uuid.uuid4(),
                tenant_id=tenant,
                range_id=rng.id,
                action="provision",
                generation=1,
                request_hash="h",
                status="pending",
            )
            s.add(op)
            s.commit()
        results: dict = {}

        def process(label):
            with make() as s:
                results[label] = range_ops.dispatch(s, s.get(RangeOperation, op.id))

        threads = [threading.Thread(target=process, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        assert sends == ["provision_range"], f"sent {len(sends)} times"
        assert sorted(results.values()) == [False, True]
        with make() as s:
            stored = s.get(RangeOperation, op.id)
            assert (stored.status, stored.dispatch_attempts) == ("dispatched", 1)
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
