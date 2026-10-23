"""The content clock's row locks against the worker's sync, on PostgreSQL (ADR 0014, #286).

SQLite ignores row locks, so these run only with TEST_POSTGRES_ADMIN_URL (CI's test-python
job; ``postgres_engine`` in tests/conftest.py is a fresh copy of ``alembic upgrade head``).

Two deadlocks the review of PR #295 reproduced on PostgreSQL 16:

1. The sync inserts edges; their foreign keys take FOR KEY SHARE on the review rows they
   name. A Mark reviewed that locked its rows FOR UPDATE then waited on such a row while
   holding a lower one the sync's own write step wanted next. Both sides now lock FOR NO KEY
   UPDATE, which a foreign key check does not block.
2. Retire locked the item first, then the item and its dependents in id order: a dependent
   with a lower id, locked meanwhile by the sync (also in id order), closed the circle. It
   now takes its locks in one id-ordered statement, as Mark reviewed does.

Each race is staged with a pause that holds one side at the point of conflict for longer
than PostgreSQL's deadlock_timeout (1 s), so a deadlock, if there is one, is reported.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import CurrentUser
from app.content_review import ContentReviewItem, service
from app.content_review import router as review_router
from app.content_review.schemas import RetireIn
from app.models import Course, Scenario, Template, Tenant, User, UserRole
from app.scheduler.models import EventState, ScheduledEvent
from sqlalchemy.orm import Session
from worker import content_review_sync as sync_mod

pytestmark = pytest.mark.slow

NOW = datetime.now(UTC)
HOLD = 2.5  # seconds one side waits at the point of conflict: longer than deadlock_timeout
# Review row ids chosen so the scenario sorts before the template, and the course last.
S_ID = uuid.UUID("10000000-0000-4000-8000-000000000000")
T_ID = uuid.UUID("20000000-0000-4000-8000-000000000000")
C_ID = uuid.UUID("30000000-0000-4000-8000-000000000000")


@pytest.fixture
def world(postgres_engine):
    """Template T, scenario S on it, course C; review rows for all three, the sync's edges,
    and T flagged stale (so S carries its reason)."""
    eng = postgres_engine
    tenant, admin, tpl, scn, course = (uuid.uuid4() for _ in range(5))
    with Session(eng) as db:
        db.add(Tenant(id=tenant, name="T", slug=f"t-{tenant.hex[:6]}"))
        db.flush()
        db.add(User(id=admin, email="a@example.test", display_name="Admin", role=UserRole.admin, tenant_id=tenant,
                    keycloak_id=f"kc-{admin.hex[:8]}"))
        db.add(Template(id=tpl, name="Medium Enterprise", yaml=json.dumps({"name": "Medium Enterprise", "nodes": []}),
                        tenant_id=tenant))
        db.add(Scenario(id=scn, name="SOC", yaml=json.dumps({"name": "SOC", "range_template": "Medium Enterprise"}),
                        tenant_id=tenant))
        db.add(Course(id=course, name="C201", tenant_id=tenant))
        db.flush()
        for rid, item_type, item_id, title in ((S_ID, "scenario", scn, "SOC"), (T_ID, "range_template", tpl,
                                                "Medium Enterprise"), (C_ID, "course", course, "C201")):
            db.add(ContentReviewItem(id=rid, tenant_id=tenant, item_type=item_type, item_id=str(item_id), title=title,
                                     theme_ids=[], stale_reasons=[], due_at=NOW + timedelta(days=90)))
        db.commit()
    with Session(eng) as db:
        sync_mod.sync(db)
        db.commit()
    with Session(eng) as db:
        service.flag_stale(db, T_ID, "Known-exploited vulnerability", "kev:1", kind="vulnerability")
        db.commit()
    user = CurrentUser(id=str(admin), email="a@example.test", display_name="Admin", role=UserRole.admin,
                       tenant_id=str(tenant), keycloak_id=f"kc-{admin.hex[:8]}")
    return {"engine": eng, "tenant": tenant, "user": user, "template": tpl, "scenario": scn, "course": course}


def _race(first, second) -> list[BaseException]:
    """Run both, each in its own thread; return what either raised."""
    errors: list[BaseException] = []

    def guard(fn):
        def run():
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 -- reported by the test
                errors.append(exc)
        return run

    threads = [threading.Thread(target=guard(f)) for f in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a side never finished (a lock wait with no deadlock check?)"
    return errors


def test_mark_reviewed_while_the_sync_adds_an_edge(world, monkeypatch):
    eng = world["engine"]
    with Session(eng) as db:  # what the next sync will find: a booking (course C -> template T, a new edge)
        db.add(ScheduledEvent(id=uuid.uuid4(), name="Class", tenant_id=world["tenant"], course_id=world["course"],
                              template_id=world["template"], state=EventState.scheduled, start_time=NOW,
                              end_time=NOW + timedelta(hours=8)))
        db.query(Scenario).filter(Scenario.id == world["scenario"]).update({"name": "SOC v2"})  # S is written too
        db.commit()
    edges_in = threading.Event()
    real = sync_mod.st.Graph.repropagate

    def after_the_edges(graph, **kw):  # step 2 (edges inserted, uncommitted) is done
        edges_in.set()
        time.sleep(HOLD)  # Mark reviewed now locks S, then T
        return real(graph, **kw)

    monkeypatch.setattr(sync_mod.st.Graph, "repropagate", after_the_edges)

    def run_sync():
        with Session(eng) as db:
            sync_mod.sync(db)
            db.commit()

    def review():
        assert edges_in.wait(30)
        with Session(eng) as db:
            row = service.get_visible(db, world["user"], T_ID)
            service.mark_reviewed(db, world["user"], row)
            db.commit()

    errors = _race(run_sync, review)
    assert not errors, errors
    with Session(eng) as db:
        t = db.get(ContentReviewItem, T_ID)  # tenant-safe: test
        assert t.last_reviewed_at is not None and not t.stale


def test_retire_while_the_sync_locks_overlapping_rows(world, monkeypatch):
    eng = world["engine"]
    with Session(eng) as db:  # the next sync writes S and T (renamed)
        db.query(Scenario).filter(Scenario.id == world["scenario"]).update({"name": "SOC v2"})
        db.query(Template).filter(Template.id == world["template"]).update({"name": "Medium Enterprise v2"})
        db.commit()
    item_read = threading.Event()
    real_load = service.load_graph

    def slow_load(db, key):  # Retire has read (and, before the fix, locked) T: the sync goes now
        item_read.set()
        time.sleep(HOLD)
        return real_load(db, key)

    monkeypatch.setattr(service, "load_graph", slow_load)

    def retire():
        with Session(eng) as db:
            review_router.retire(RetireIn(reason="replaced"), item_id=T_ID, db=db, user=world["user"])

    def run_sync():
        assert item_read.wait(30)
        with Session(eng) as db:
            sync_mod.sync(db)
            db.commit()

    errors = _race(retire, run_sync)
    assert not errors, errors
    with Session(eng) as db:
        assert db.get(ContentReviewItem, T_ID).retired  # tenant-safe: test
