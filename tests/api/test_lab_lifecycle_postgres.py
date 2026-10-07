"""Lab sessions under concurrency, on PostgreSQL (R3: CR1-12, CR1-16; F15 in
docs/review/codereview1.md). Skipped without TEST_POSTGRES_ADMIN_URL.

* CR1-12: a launch locked only the launching student's own row, then counted the tenant's
  running labs. Two students launching at once each counted before either lab was
  written, and together went past the tenant's limits.
* CR1-16: the lab lease had no owner. A run whose lease lapsed between its commit and its
  send (a slow broker) still sent its tasks after another process had taken the lab and
  sent them: the same task twice.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from _release_kit import CATALOGUE, CROSSWALK, build
from app.db import get_db
from app.lab_sessions import service
from app.lab_sessions.models import LabSession
from app.main import app as fastapi_app
from app.models import GoldenImage, Tenant, User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

DEV = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def pg_lab(postgres_engine, tmp_path, monkeypatch):
    """The lab fixture of test_lab_sessions.py, on a migrated PostgreSQL database."""
    monkeypatch.setenv("PROVISIONER_BACKEND", "mock")
    with Session(postgres_engine) as s:
        s.add(Tenant(id=DEV, name="dev", slug="dev"))
        s.flush()
        s.add(User(id=DEV, email="a@x.test", display_name="a", role=UserRole.admin, tenant_id=DEV, keycloak_id="dev"))
        s.add(GoldenImage(catalogue_id="ubuntu-lts", hypervisor="vsphere", template_name="ubuntu-2404", enabled=True))
        s.commit()

    def _get_db():
        db = Session(postgres_engine)
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = _get_db
    try:
        with TestClient(fastapi_app) as client:
            client.post("/qsp/import-crosswalk", files={"file": ("c.csv", CROSSWALK.read_bytes(), "text/csv")})
            client.post("/courses/import-programme", files={"file": ("c.csv", CATALOGUE.read_bytes(), "text/csv")})
            data = build(tmp_path, range_ordinals=frozenset({6}))
            resp = client.post("/course-releases", files={"file": ("release.tar.gz", data, "application/gzip")})
            rid = resp.json()["id"]
            assert client.post(f"/course-releases/{rid}/accept", json={}).status_code == 200
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
    return uuid.UUID(rid)


def _students(engine, n: int) -> list[uuid.UUID]:
    with Session(engine) as s:
        users = [
            User(
                email=f"s{uuid.uuid4().hex[:8]}@x.test",
                display_name="s",
                role=UserRole.student,
                tenant_id=DEV,
                keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
            )
            for _ in range(n)
        ]
        s.add_all(users)
        s.commit()
        return [u.id for u in users]


def test_on_postgres_concurrent_launches_never_exceed_the_tenant_limit(postgres_engine, pg_lab, monkeypatch):
    monkeypatch.setenv("LAB_MAX_SESSIONS_PER_TENANT", "2")
    monkeypatch.setattr(service, "_dispatch", lambda task, *args: "task-1")
    users = _students(postgres_engine, 4)
    barrier = threading.Barrier(len(users))
    errors: list[BaseException] = []

    def launch(user_id):
        try:
            with Session(postgres_engine) as db:
                barrier.wait()
                service.launch(db, tenant_id=DEV, user_id=user_id, release_id=pg_lab, activity_id="mod_006")
                db.commit()
                service.flush_outbox(db)
        except BaseException as exc:  # noqa: BLE001 — reported below
            errors.append(exc)

    threads = [threading.Thread(target=launch, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    with Session(postgres_engine) as s:
        states = sorted(x.state for x in s.query(LabSession).all())
    assert len(states) == 4
    assert states.count("provisioning") == 2, f"the tenant limit is 2: {states}"
    assert states.count("queued") == 2


def test_on_postgres_a_sweep_during_a_send_whose_lease_lapsed_does_not_send_again(postgres_engine, pg_lab, monkeypatch):
    """The first process's send is slow and its lease lapses. Another process's sweep runs
    while the send is in flight: it must not take the lab and send the same task again."""
    (user_id,) = _students(postgres_engine, 1)
    sent: list[str] = []
    sweeps: list[threading.Thread] = []

    def other_process_sweeps():
        with Session(postgres_engine) as second:
            service.sweep(second)

    def slow_send(task, *args):
        sent.append(task)
        if len(sent) == 1:  # the first process's send: another process sweeps meanwhile
            sweeper = threading.Thread(target=other_process_sweeps)
            sweeps.append(sweeper)
            sweeper.start()
            sweeper.join(3)  # with the fix it waits on the first process's lease: let it
        return "task-1"

    monkeypatch.setattr(service, "_dispatch", slow_send)
    with Session(postgres_engine) as first:
        lab, _ = service.launch(first, tenant_id=DEV, user_id=user_id, release_id=pg_lab, activity_id="mod_006")
        lab_id = lab.id
        first.commit()
        with postgres_engine.begin() as conn:  # the lease lapses before the send
            conn.execute(
                text("UPDATE lab_sessions SET lease_until = now() - interval '1 second' WHERE id = :i"), {"i": lab_id}
            )
        service.flush_outbox(first)
    for sweeper in sweeps:
        sweeper.join(10)
    assert sent == ["provision_range"], f"the same task was sent {len(sent)} times"
    with Session(postgres_engine) as s:
        row = s.get(LabSession, lab_id)
        assert row.pending == "[]" and row.lease_holder is None


def test_on_postgres_a_stale_run_that_already_read_its_tasks_does_not_send_them_again(
    postgres_engine, pg_lab, monkeypatch
):
    """The interleaving only the holder check stops (review of 6558791): the first run has
    read its pending tasks when another process claims the lapsed lease, sends them and
    lets the lease go, all before the first run's send. Without ``lease_holder == mine``
    the first run renewed a lease that was no longer its own and sent them again."""
    (user_id,) = _students(postgres_engine, 1)
    sent: list[str] = []
    monkeypatch.setattr(service, "_dispatch", lambda task, *args: sent.append(task) or "task-1")
    with Session(postgres_engine) as first:
        lab, _ = service.launch(first, tenant_id=DEV, user_id=user_id, release_id=pg_lab, activity_id="mod_006")
        lab_id = lab.id
        first.commit()
        assert lab.pending != "[]"  # the first run has read its tasks (they stay in its session)
        with postgres_engine.begin() as conn:
            conn.execute(
                text("UPDATE lab_sessions SET lease_until = now() - interval '1 second' WHERE id = :i"), {"i": lab_id}
            )
        with Session(postgres_engine) as second:
            service.sweep(second)  # claims, sends, releases: done before the first run's send
        assert sent == ["provision_range"]
        service.flush_outbox(first)
    assert sent == ["provision_range"], f"the same task was sent {len(sent)} times"
