"""Companion oracles for the confirmed audit findings of #443 (card #444).

One companion per strict-xfail oracle in ``tests/api/test_audit_classes_443.py``: a second
input that pins the same secure behaviour from another angle — a second role, a boundary
value, a two-class matrix or the API surface instead of the service call. Each FAILS at the
base for the same reason its oracle does, and is ``xfail(strict=True)`` so the later fix
turns it into an XPASS until its marker is dropped. The donor fixtures and helpers come
from the oracle module by module path (tests/api has no ``__init__.py``; never
``from conftest import``).

Verify a companion still bites with ``--runxfail``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from _shared import act_as, real_user
from app.classes import service as class_service
from app.classes.models import Class, ClassStatus, RollCallEntry
from app.models import UserRole
from app.scheduler.models import ScheduledEvent
from test_audit_classes_443 import (
    DAY,
    _approve,
    _book,
    _class_body,
    _fill,
    _identity,
    _on_roll_call,
    cluster,  # noqa: F401 -- fixture, requested by name through `world`
    outbox,  # noqa: F401 -- fixture, requested by name through `world`
    seats,  # noqa: F401 -- fixture, requested by name
    sign_in,  # noqa: F401 -- fixture, requested by name
    world,  # noqa: F401 -- fixture, requested by name
)

# The imported fixture objects are only ever requested by name (as fixture parameters),
# which the gate's breakage rules would read as an unused import being redefined; keep
# the bindings genuinely used here.
_DONOR_FIXTURES = (cluster, outbox, seats, sign_in, world)


# ── 1. Re-dating stretches the hold with no re-approval: the boundary ─────
# Companion to test_audit_classes_443.py::test_f1_... (probes day 100 of a 200-day
# stretch). Here the class is re-dated by a single day and the probe is the *first* hour
# past the approved plan: the smallest stretch there is, so even a one-day re-date must
# not quietly take the next morning.


@pytest.mark.audit443
def test_companion_f1_redating_by_one_day_already_reaches_past_the_approved_plan(client, world):
    ev = _book(client, world)  # approved session DAY 09:00-12:00 UTC (shared template)
    _approve(client, world, ev)
    # The first hour of the day after the class's original end_date (DAY + 4): past
    # last_day_end(DAY+4) + teardown grace (15 min), so only a stretched hold reaches it.
    probe = {"start_time": (DAY + timedelta(days=5, hours=9)).isoformat(),
             "end_time": (DAY + timedelta(days=5, hours=10)).isoformat()}
    act_as(world["admin"])
    assert client.get("/schedule/capacity", params=probe).json()["overlapping_events"] == 0
    act_as(world["lead"])
    extended = _class_body(world, end_date=str((DAY + timedelta(days=5)).date()))
    r = client.put(f"/classes/{world['class']['id']}", json=extended)
    # Only a legitimate outcome may silence the oracle: the re-dating lands (200) or is
    # refused (409) — any other break in PUT /classes must keep this test red.
    assert r.status_code in (200, 409), r.text
    act_as(world["admin"])
    held = client.get("/schedule/capacity", params=probe).json()
    assert held["overlapping_events"] == 0, (
        "a one-day re-date must not stretch the hold past the approved plan either"
    )


# ── 2. Cancel racing Close-out: the lead's cancel ─────────────────────────
# Companion to test_f2_... (an admin cancels). Here the class's own lead cancels — the
# other editor the route allows — under the same replayed close.


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Cancel racing Close-out overwrites a "
                                        "closed class to cancelled (classes/router.py:384) — companion")
def test_companion_f2_the_leads_cancel_may_not_overwrite_a_class_closed_concurrently(
    client, world, db_session, monkeypatch,
):
    cid = uuid.UUID(world["class"]["id"])
    real_audit = class_service.audit

    def close_lands_while_cancel_is_in_flight(db, *args, **kwargs):
        db.query(Class).filter(Class.id == cid).update(
            {Class.status: ClassStatus.closed.value}, synchronize_session=False
        )
        return real_audit(db, *args, **kwargs)

    monkeypatch.setattr(class_service, "audit", close_lands_while_cancel_is_in_flight)
    act_as(world["lead"])
    client.delete(f"/classes/{cid}")
    db_session.expire_all()
    assert db_session.get(Class, cid).status == ClassStatus.closed.value, (
        "a lead's cancel that raced the close-out must not overwrite closed with cancelled"
    )


# ── 3. Reject racing Approve: through the API route ───────────────────────
# Companion to test_f3_... (calls service.reject_addition directly). Here the same race is
# replayed against POST /classes/{id}/roll-call/{entry_id}/reject — the route an approver
# really calls, which commits — so the entry must survive the commit as invited.


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Reject of a pending roll-call addition is "
                                        "unguarded: racing an Approve it removes an admitted "
                                        "Student without freeing the seat (classes/service.py:476) — companion")
def test_companion_f3_the_reject_route_may_not_remove_a_student_approved_concurrently(
    client, world, db_session, seats,
):
    _approve(client, world, _book(client, world, template="", vcpu_total=40))
    _fill(db_session, world["b"].id, vcpu=55)  # another tenant takes most of the cluster
    _on_roll_call(client, world, "late@corp.test")  # 40 + 55 + 10 > 100 → held for approval
    entry = db_session.query(RollCallEntry).filter_by(email="late@corp.test").one()
    assert entry.status == "pending"
    # The filler goes before the reject runs, so a fixed route's 409 can only be the race
    # guard, not a capacity refusal.
    db_session.query(ScheduledEvent).filter(ScheduledEvent.name == "someone else").delete()
    db_session.flush()
    # The concurrent Approve commits: pending → invited (its CAS) and the seat is taken.
    db_session.query(RollCallEntry).filter(RollCallEntry.id == entry.id).update(
        {RollCallEntry.status: "invited"}, synchronize_session=False
    )
    act_as(world["admin"])
    r = client.post(
        f"/classes/{world['class']['id']}/roll-call/{entry.id}/reject",
        json={"reason": "duplicate request"},
    )
    assert r.status_code == 409, r.text
    db_session.expire_all()
    assert db_session.get(RollCallEntry, entry.id).status == "invited", (
        "a reject through the route that raced the approve must not remove the admitted Student"
    )


# ── 4. Course switched behind its sessions: the empty roll call ───────────
# Companion to test_f4_... (an invited-only roll call). Here the roll call is empty: no
# Student is linked or invited, so the guard the finding names (_has_linked_students) has
# nothing to hold on to — the booked, approved session alone must pin the course.


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: A class's course can be changed after its "
                                        "sessions were booked; the sessions keep the old "
                                        "course_id (classes/service.py:150) — companion")
def test_companion_f4_a_booked_session_pins_the_course_even_with_an_empty_roll_call(
    client, world, db_session,
):
    ev = _book(client, world)
    _approve(client, world, ev)  # nobody is on the roll call at all
    act_as(world["lead"])
    switched = _class_body(world, course_id=str(world["other_course"].id))
    r = client.put(f"/classes/{world['class']['id']}", json=switched)
    # Only a legitimate outcome may silence the oracle: the switch lands (200) or is
    # refused (409) — any other break in PUT /classes must keep this test red.
    assert r.status_code in (200, 409), r.text
    db_session.expire_all()
    cls = db_session.get(Class, uuid.UUID(world["class"]["id"]))
    evt = db_session.get(ScheduledEvent, uuid.UUID(ev["id"]))
    assert cls.course_id == evt.course_id, (
        "a booked session pins the course even when nobody is on the roll call yet"
    )


# ── 5. A demoted assistant keeps the class: the observer ──────────────────
# Companion to test_f5_... (a demoted assistant). Here a demoted *observer* — the other
# staff role the finding names — on a second class of the same tenant.


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: A former assistant/observer demoted to "
                                        "Student keeps the class in GET /classes "
                                        "(classes/access.py:35) — companion")
def test_companion_f5_a_demoted_observer_no_longer_sees_the_class_in_the_list(client, world, db_session):
    observer = real_user(db_session, UserRole.observer, world["a"].id)
    act_as(world["lead"])
    r = client.post("/classes", json=_class_body(world, name="C201-26-45", observer_ids=[observer.id]))
    assert r.status_code == 201, r.text
    watched = r.json()["id"]
    demoted = observer.model_copy(update={"role": UserRole.student})
    act_as(demoted)
    listed = [c["id"] for c in client.get("/classes").json()]
    assert watched not in listed, (
        "a Student who is no longer an observer on the class must not see it in the list"
    )


# ── 6. Admission gates on the UTC day: two classes, one site evening ──────
# Companion to test_f6_... (one class, sign-in refused). Here two classes of the same
# tenant share the sign-in's email: one is on its last site evening (must admit), one
# ended the site day before (must not) — the fix must claim exactly the running one.


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Admission uses the UTC calendar day for "
                                        "the class's last day while the rest of the area uses "
                                        "the site zone (classes/onboarding.py:100) — companion")
def test_companion_f6_sign_in_on_the_last_site_day_admits_only_the_class_still_running(
    client, world, db_session, sign_in, monkeypatch,
):
    from app.classes import onboarding

    monkeypatch.setenv("SITE_TIMEZONE", "America/Chicago")
    # 2026-06-01 03:30 UTC = 2026-05-31 22:30 in Chicago: the site's last evening of the
    # running class. Only the admission gate's clock is frozen; everything else runs for real.
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: A003 - the datetime API
            return datetime(2026, 6, 1, 3, 30, tzinfo=UTC)

    monkeypatch.setattr(onboarding, "datetime", FrozenDatetime)
    running = db_session.get(Class, uuid.UUID(world["class"]["id"]))
    running.start_date, running.end_date = date(2026, 5, 27), date(2026, 5, 31)
    act_as(world["lead"])
    r = client.post("/classes", json=_class_body(world, name="C201-26-46"))
    assert r.status_code == 201, r.text
    ended = db_session.get(Class, uuid.UUID(r.json()["id"]))
    ended.start_date, ended.end_date = date(2026, 5, 24), date(2026, 5, 30)
    db_session.flush()
    _on_roll_call(client, world, "joe.bloggs@corp.test")
    act_as(world["lead"])
    r = client.post(f"/classes/{ended.id}/roll-call", json={"emails": ["joe.bloggs@corp.test"]})
    assert r.status_code == 200, r.text
    body = sign_in(_identity())
    assert body["status"] == "registered", (
        "on the running class's last site day the sign-in must still admit, not refuse"
    )
    db_session.expire_all()
    claimed = (
        db_session.query(RollCallEntry)
        .filter(RollCallEntry.email == "joe.bloggs@corp.test", RollCallEntry.status == "signed_in")
        .all()
    )
    assert [e.class_id for e in claimed] == [running.id], (
        "the sign-in joins the class still running on the site's day, never the one that ended"
    )


# ── 1. Re-dating: what the fix must still allow ────────────────────────────


def test_an_admin_may_still_extend_a_class_with_approved_sessions(client, world):
    """Guard for the router.py:349 fix: the refusal is the lead's; an admin approves holds."""
    ev = _book(client, world)
    _approve(client, world, ev)
    act_as(world["admin"])
    r = client.put(f"/classes/{world['class']['id']}",
                   json=_class_body(world, end_date=str((DAY + timedelta(days=9)).date())))
    assert r.status_code == 200, r.text


def test_the_lead_may_extend_a_class_that_has_no_approved_session(client, world):
    """Guard: with nothing approved there is no hold to stretch, so the lead may re-date."""
    act_as(world["lead"])
    r = client.put(f"/classes/{world['class']['id']}",
                   json=_class_body(world, end_date=str((DAY + timedelta(days=9)).date())))
    assert r.status_code == 200, r.text


def test_the_lead_may_still_shorten_or_rename_a_class_with_approved_sessions(client, world):
    """Guard: only moving the end date later is refused; same-or-earlier and other edits land."""
    ev = _book(client, world)
    _approve(client, world, ev)
    act_as(world["lead"])
    for end in (DAY + timedelta(days=4), DAY + timedelta(days=3)):
        r = client.put(f"/classes/{world['class']['id']}",
                       json=_class_body(world, name="renamed", end_date=str(end.date())))
        assert r.status_code == 200, r.text
