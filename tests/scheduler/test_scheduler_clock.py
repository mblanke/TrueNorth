"""The scheduler clock (ADR 0004 slice 4).

Provision at the lead, activate at the start, complete and tear down after the grace,
remind the instructor once. Each step happens once however often the clock ticks, and
the clock only tears down a range it built.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Range, RangeState, User, UserRole
from app.scheduler import clock
from app.scheduler.models import EventState, ScheduledEvent

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
START = (datetime.now(UTC) + timedelta(days=60)).replace(hour=13, minute=0, second=0, microsecond=0)
END = START + timedelta(hours=3)
LEAD, GRACE = timedelta(minutes=30), timedelta(minutes=15)


@pytest.fixture
def dispatched():
    """Every worker task the range lifecycle sends, as (name, args)."""
    sent: list[tuple[str, tuple]] = []

    def fake(name, *args):
        sent.append((name, args))
        return f"task-{len(sent)}"

    with patch("app.celery_client.dispatch", side_effect=fake):
        yield sent


def _range(db, state: RangeState = RangeState.created) -> Range:
    r = Range(id=uuid.uuid4(), tenant_id=uuid.UUID(DEV_TENANT), name="r", template_id=uuid.uuid4())
    r.state = state
    db.add(r)
    db.flush()
    return r


def _teacher(db, email: str = "teacher@example.test") -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=email,
        display_name="Teacher",
        role=UserRole.instructor,
        tenant_id=uuid.UUID(DEV_TENANT),
    )
    db.add(u)
    db.flush()
    return u


def _booking(db, *, rng: Range | None = None, state=EventState.scheduled, instructor: User | None = None):
    ev = ScheduledEvent(
        name="Blue team drill",
        state=state,
        tenant_id=uuid.UUID(DEV_TENANT),
        range_id=rng.id if rng else None,
        instructor_id=instructor.id if instructor else None,
        start_time=START,
        end_time=END,
    )
    db.add(ev)
    db.flush()
    return ev


def _state(db, obj):
    db.refresh(obj)
    return obj.state


def test_nothing_happens_before_the_lead(db_session, dispatched):
    ev = _booking(db_session, rng=_range(db_session))
    clock.tick(db_session, now=START - LEAD - timedelta(minutes=1))
    assert _state(db_session, ev) == EventState.scheduled
    assert dispatched == []


def test_the_whole_session_end_to_end(db_session, dispatched):
    rng = _range(db_session)
    ev = _booking(db_session, rng=rng)

    r = clock.tick(db_session, now=START - LEAD)
    assert r.provisioning == 1
    assert _state(db_session, ev) == EventState.provisioning
    assert ev.auto_provisioned is True
    assert _state(db_session, rng) == RangeState.provisioning
    assert dispatched == [("provision_range", (str(rng.id),))]

    rng.state = RangeState.ready  # the worker finished
    db_session.flush()
    assert clock.tick(db_session, now=START).activated == 1
    assert _state(db_session, ev) == EventState.active

    clock.tick(db_session, now=END + GRACE - timedelta(seconds=1))
    assert _state(db_session, ev) == EventState.active  # still in its grace

    r = clock.tick(db_session, now=END + GRACE)
    assert (r.completed, r.torn_down) == (1, 1)
    assert _state(db_session, ev) == EventState.completed
    assert _state(db_session, rng) == RangeState.destroying
    assert dispatched[-1] == ("destroy_range", (str(rng.id),))


def test_ticking_again_does_nothing_twice(db_session, dispatched):
    _booking(db_session, rng=_range(db_session))
    for _ in range(3):
        clock.tick(db_session, now=START - LEAD + timedelta(minutes=1))
    assert [name for name, _ in dispatched] == ["provision_range"]


def test_a_range_that_was_already_up_is_used_and_left_up(db_session, dispatched):
    rng = _range(db_session, RangeState.ready)
    ev = _booking(db_session, rng=rng)
    clock.tick(db_session, now=START - LEAD)
    assert _state(db_session, ev) == EventState.provisioning
    assert ev.auto_provisioned is False
    clock.tick(db_session, now=START)
    clock.tick(db_session, now=END + GRACE)
    assert _state(db_session, ev) == EventState.completed
    assert _state(db_session, rng) == RangeState.ready
    assert dispatched == []


def test_a_missed_session_is_closed_without_building_anything(db_session, dispatched):
    """The API was down for the whole window: catch up in one tick, build nothing."""
    ev = _booking(db_session, rng=_range(db_session))
    r = clock.tick(db_session, now=END + timedelta(hours=1))
    assert (r.provisioning, r.activated, r.completed, r.torn_down) == (0, 1, 1, 0)
    assert _state(db_session, ev) == EventState.completed
    assert dispatched == []


def test_drafts_and_cancelled_bookings_are_left_alone(db_session, dispatched):
    draft = _booking(db_session, rng=_range(db_session), state=EventState.draft)
    gone = _booking(db_session, rng=_range(db_session), state=EventState.cancelled)
    clock.tick(db_session, now=START)
    assert (_state(db_session, draft), _state(db_session, gone)) == (EventState.draft, EventState.cancelled)
    assert dispatched == []


def test_the_instructor_is_reminded_once(db_session, dispatched):
    teacher = _teacher(db_session)
    ev = _booking(db_session, instructor=teacher)
    assert clock.tick(db_session, now=START - timedelta(hours=25)).reminders == []
    first = clock.tick(db_session, now=START - timedelta(hours=23)).reminders
    again = clock.tick(db_session, now=START - timedelta(hours=22)).reminders
    assert [r.to for r in first] == ["teacher@example.test"]
    assert first[0].subject.startswith("Reminder: Blue team drill")
    assert again == []
    db_session.refresh(ev)
    assert ev.reminded_at is not None


def test_no_instructor_no_reminder(db_session, dispatched):
    _booking(db_session)
    assert clock.tick(db_session, now=START - timedelta(hours=1)).reminders == []


@pytest.mark.asyncio
async def test_reminders_go_out_through_the_email_channel():
    sent = []

    class FakeEmail:
        async def send(self, to, subject, body, metadata=None):
            sent.append((to, subject))
            return True

    with patch("app.notifications.get_channel", return_value=FakeEmail()):
        await clock.send_reminders([clock.Reminder("e1", "t@example.test", "Reminder: x", "body")])
    assert sent == [("t@example.test", "Reminder: x")]


# -- Through the API ------------------------------------------------------------


@contextmanager
def acting_as(role: UserRole):
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email=f"{role.value}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=DEV_TENANT,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def test_cancelling_once_the_range_is_up_tears_it_down(client, db_session, dispatched):
    rng = _range(db_session)
    ev = _booking(db_session, rng=rng)
    clock.tick(db_session, now=START - LEAD)
    rng.state = RangeState.ready
    db_session.flush()
    with acting_as(UserRole.instructor):
        assert client.post(f"/schedule/events/{ev.id}/cancel").status_code == 200
    assert dispatched[-1] == ("destroy_range", (str(rng.id),))
    assert _state(db_session, rng) == RangeState.destroying


def test_cancelling_mid_build_tears_down_once_the_build_finishes(client, db_session, dispatched):
    """A range still provisioning cannot be destroyed; the clock retries until it can."""
    rng = _range(db_session)
    ev = _booking(db_session, rng=rng)
    clock.tick(db_session, now=START - LEAD)
    with acting_as(UserRole.instructor):
        assert client.post(f"/schedule/events/{ev.id}/cancel").status_code == 200
    assert [n for n, _ in dispatched] == ["provision_range"]

    clock.tick(db_session, now=START - LEAD + timedelta(minutes=1))  # still building
    assert [n for n, _ in dispatched] == ["provision_range"]

    rng.state = RangeState.ready  # the worker finished
    db_session.flush()
    r = clock.tick(db_session, now=START - LEAD + timedelta(minutes=2))
    assert r.torn_down == 1
    assert [n for n, _ in dispatched] == ["provision_range", "destroy_range"]
    db_session.refresh(ev)
    assert ev.auto_provisioned is False
    clock.tick(db_session, now=START)  # settled: no second teardown
    assert [n for n, _ in dispatched] == ["provision_range", "destroy_range"]


@pytest.mark.parametrize(
    ("role", "code"), [(UserRole.admin, 200), (UserRole.instructor, 403), (UserRole.observer, 403)]
)
def test_only_admins_can_run_the_clock_by_hand(client, role, code):
    with acting_as(role):
        r = client.post("/schedule/tick")
    assert r.status_code == code
    if code == 200:
        assert set(r.json()) == {"ranges_created", "provisioning", "activated", "completed", "torn_down", "reminders"}


def _second_booking(db, rng: Range, start: datetime) -> ScheduledEvent:
    ev = ScheduledEvent(
        name="Second class",
        state=EventState.scheduled,
        tenant_id=uuid.UUID(DEV_TENANT),
        range_id=rng.id,
        start_time=start,
        end_time=start + timedelta(hours=3),
    )
    db.add(ev)
    db.flush()
    return ev


@pytest.mark.parametrize("gap", [timedelta(minutes=45), timedelta(days=7)])
def test_the_next_booking_of_a_range_inherits_it_instead_of_losing_it(db_session, dispatched, gap):
    """Back-to-back classes, or the same class next week: finishing the first must not
    destroy the range the second needs (a destroyed range cannot be built again)."""
    rng = _range(db_session)
    first = _booking(db_session, rng=rng)
    second = _second_booking(db_session, rng, END + gap)

    clock.tick(db_session, now=START - LEAD)
    rng.state = RangeState.ready
    db_session.flush()
    clock.tick(db_session, now=START)
    clock.tick(db_session, now=END + GRACE)

    assert _state(db_session, first) == EventState.completed
    assert _state(db_session, rng) == RangeState.ready  # kept up
    db_session.refresh(second)
    assert (first.auto_provisioned, second.auto_provisioned) == (False, True)
    assert [n for n, _ in dispatched] == ["provision_range"]

    # The heir tears it down when it finishes.
    clock.tick(db_session, now=second.start_time)
    clock.tick(db_session, now=second.end_time + GRACE)
    assert _state(db_session, second) == EventState.completed
    assert _state(db_session, rng) == RangeState.destroying
    assert [n for n, _ in dispatched] == ["provision_range", "destroy_range"]


def test_ownership_is_recorded_with_the_build_even_if_dispatch_then_fails(db_session):
    """auto_provisioned commits with the range's `provisioning` state, before dispatch,
    so a failure after that cannot leave a built range nobody will tear down."""
    rng = _range(db_session)
    ev = _booking(db_session, rng=rng)
    with (
        patch("app.celery_client.dispatch", side_effect=RuntimeError("broker exploded")),
        pytest.raises(RuntimeError),
    ):
        clock.tick(db_session, now=START - LEAD)
    db_session.refresh(ev)
    db_session.refresh(rng)
    assert rng.state == RangeState.provisioning
    assert ev.auto_provisioned is True
