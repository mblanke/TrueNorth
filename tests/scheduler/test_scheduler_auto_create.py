"""A booking links or creates its range and exercise (ADR 0004 slice 11, decided 2026-10-06).

- With a template and no range, the clock creates the range at the provisioning lead
  and builds it; it is torn down after the session, like any range the clock built.
- With a scenario, a pending exercise is created on the range when it is built; the
  instructor starts it. Cancelling the booking cancels that exercise if unstarted.
- A linked exercise is used as it is, and brings its range.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Exercise, ExerciseState, Range, RangeState, Scenario, Template, UserRole
from app.scheduler import clock
from app.scheduler.models import EventState, ScheduledEvent

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
START = (datetime.now(UTC) + timedelta(days=20)).replace(hour=13, minute=0, second=0, microsecond=0)
END = START + timedelta(hours=3)
LEAD, GRACE = timedelta(minutes=30), timedelta(minutes=15)
YAML = "nodes:\n  - id: ws\n    os: win11\n    count: 2\n"


@pytest.fixture
def dispatched():
    sent: list[tuple[str, tuple]] = []
    with patch("app.celery_client.dispatch", side_effect=lambda n, *a: sent.append((n, a)) or "t"):
        yield sent


@contextmanager
def acting_as(role: UserRole, tenant: str = DEV_TENANT):
    who = CurrentUser(
        id=str(uuid.uuid4()), email="i@example.test", display_name="i", role=role, tenant_id=tenant, keycloak_id="kc"
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _template(db) -> Template:
    t = Template(name="Two workstations", yaml=YAML, tenant_id=uuid.UUID(DEV_TENANT))
    db.add(t)
    db.flush()
    return t


def _scenario(db, tenant: str = DEV_TENANT, public: bool = False) -> Scenario:
    sc = Scenario(name="Phishing triage", yaml="name: x\n", tenant_id=uuid.UUID(tenant))
    sc.is_public = public
    db.add(sc)
    db.flush()
    return sc


def _booking(db, name: str = "Blue team drill", **kw) -> ScheduledEvent:
    ev = ScheduledEvent(
        name=name,
        state=EventState.scheduled,
        tenant_id=uuid.UUID(DEV_TENANT),
        start_time=START,
        end_time=END,
        **kw,
    )
    db.add(ev)
    db.flush()
    return ev


def _fresh(db, obj):
    db.refresh(obj)
    return obj


def test_a_booking_with_a_template_but_no_range_gets_one_built(db_session, dispatched):
    tpl = _template(db_session)
    ev = _booking(db_session, template_id=tpl.id)

    assert clock.tick(db_session, now=START - LEAD - timedelta(minutes=1)).ranges_created == 0
    r = clock.tick(db_session, now=START - LEAD)
    assert (r.ranges_created, r.provisioning) == (1, 1)

    ev = _fresh(db_session, ev)
    rng = db_session.get(Range, ev.range_id)
    assert rng.template_id == tpl.id and str(rng.tenant_id) == DEV_TENANT
    assert rng.state == RangeState.provisioning
    assert ev.auto_provisioned is True
    assert dispatched == [("provision_range", (str(rng.id),))]

    # ...and it goes when the session is over, like any range the clock built.
    rng.state = RangeState.ready
    db_session.flush()
    clock.tick(db_session, now=START)
    clock.tick(db_session, now=END + GRACE)
    assert _fresh(db_session, rng).state == RangeState.destroying


def test_ticking_again_creates_one_range_only(db_session, dispatched):
    _booking(db_session, template_id=_template(db_session).id)
    for _ in range(3):
        clock.tick(db_session, now=START - LEAD)
    assert db_session.query(Range).count() == 1


def test_a_scenario_gets_a_pending_exercise_on_the_built_range(db_session, dispatched):
    sc = _scenario(db_session)
    ev = _booking(db_session, template_id=_template(db_session).id, scenario_id=sc.id)
    clock.tick(db_session, now=START - LEAD)
    ev = _fresh(db_session, ev)
    ex = db_session.get(Exercise, ev.exercise_id)
    assert ex.range_id == ev.range_id and ex.scenario_id == sc.id
    assert ex.state == ExerciseState.pending  # the instructor starts it
    assert ev.auto_exercise is True


def test_cancelling_withdraws_an_unstarted_exercise_but_never_a_running_one(client, db_session, dispatched):
    sc = _scenario(db_session)
    a = _booking(db_session, template_id=_template(db_session).id, scenario_id=sc.id)
    b = _booking(db_session, template_id=_template(db_session).id, scenario_id=sc.id, name="Second")
    clock.tick(db_session, now=START - LEAD)
    a, b = _fresh(db_session, a), _fresh(db_session, b)
    running = db_session.get(Exercise, b.exercise_id)
    running.state = ExerciseState.running
    db_session.flush()
    with acting_as(UserRole.admin):
        assert client.post(f"/schedule/events/{a.id}/cancel").status_code == 200
        assert client.post(f"/schedule/events/{b.id}/cancel").status_code == 200
    assert _fresh(db_session, db_session.get(Exercise, a.exercise_id)).state == ExerciseState.cancelled
    assert _fresh(db_session, running).state == ExerciseState.running


def test_a_linked_exercise_brings_its_range_and_is_used_as_it_is(client, db_session, dispatched):
    rng = Range(id=uuid.uuid4(), tenant_id=uuid.UUID(DEV_TENANT), name="r", template_id=uuid.uuid4())
    rng.state = RangeState.created
    db_session.add(rng)
    db_session.flush()
    ex = Exercise(id=uuid.uuid4(), name="Existing", range_id=rng.id, tenant_id=uuid.UUID(DEV_TENANT))
    db_session.add(ex)
    db_session.flush()
    body = {"name": "Linked", "start_time": START.isoformat(), "end_time": END.isoformat(), "exercise_id": str(ex.id)}
    with acting_as(UserRole.instructor):
        created = client.post("/schedule/events", json=body).json()
        clash = client.post("/schedule/events", json={**body, "name": "Wrong range", "range_id": str(uuid.uuid4())})
    assert created["range_id"] == str(rng.id) and created["exercise_id"] == str(ex.id)
    assert clash.status_code in (404, 422)
    clock.tick(db_session, now=START - LEAD)
    assert db_session.query(Exercise).count() == 1  # nothing new created


def test_scenarios_must_be_the_tenants_own_or_public(client, db_session):
    theirs = _scenario(db_session, tenant=OTHER_TENANT)
    shared = _scenario(db_session, tenant=OTHER_TENANT, public=True)
    body = {"name": "x", "start_time": START.isoformat(), "end_time": END.isoformat()}
    with acting_as(UserRole.instructor):
        assert client.post("/schedule/events", json={**body, "scenario_id": str(theirs.id)}).status_code == 404
        assert client.post("/schedule/events", json={**body, "scenario_id": str(shared.id)}).status_code == 201
