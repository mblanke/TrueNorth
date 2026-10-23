"""Classes 4: class booking approval (ADR 0014 §14-17; issue #281).

The rules under test:

- A class session is booked ``pending_approval``. It holds nothing (capacity, range,
  instructor), the clock never builds, starts or reminds it, and nobody is invited. One
  nobody approves before it ends lapses. Bookings with no class stay self-service.
- Only the class's lead or an admin books its sessions; an approver (admin, range ops:
  ``schedule:approve``) approves, rejects with a reason, or proposes another time; the
  session's instructor accepts (re-checked, moved, approved) or declines a proposal.
- Approve re-checks conflicts and capacity now: 409 with the reasons and the next free
  slot under ``block``; approved with audited warnings under ``warn``.
- Approval sends the invites, to the instructor and the class's roll call only (not the
  course's other Students, not another tenant's).
- After approval, removing a Student is always allowed; adding one is automatic only
  while the sessions have room for one more seat, otherwise it waits for an approver.
- Everything is tenant-scoped, and every decision is audited.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from _shared import act_as, real_tenant, real_user
from app.classes import sessions as class_sessions
from app.classes.models import ClassSession, RollCallEntry, RollCallSettings
from app.enrollment import ensure_enrollment
from app.models import AuditLog, Course, Range, RangeState, Template, User, UserRole
from app.scheduler import clock, lifecycle
from app.scheduler.capacity import Resources
from app.scheduler.models import BookingApproval, EventState, ScheduledEvent
from fastapi import HTTPException

DAY = (datetime.now(UTC) + timedelta(days=40)).replace(hour=0, minute=0, second=0, microsecond=0)
NINE = DAY + timedelta(hours=9)


class Outbox:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, to, subject, body, metadata=None):
        method, _ = (metadata or {}).get("calendar", (None, ""))
        self.sent.append({"to": to, "subject": subject, "method": method})
        return True

    def invited(self, method: str = "REQUEST") -> set[str]:
        return {m["to"] for m in self.sent if m["method"] == method}

    def clear(self):
        self.sent.clear()


@pytest.fixture
def outbox():
    box = Outbox()
    with patch("app.notifications.get_channel", return_value=box):
        yield box


@pytest.fixture
def small_cluster(monkeypatch):
    """100 vCPU, 100 GB RAM, 1000 GB disk, no overhead."""
    monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "100")
    monkeypatch.setenv("CLUSTER_TOTAL_RAM_MB", str(100 * 1024))
    monkeypatch.setenv("CLUSTER_TOTAL_DISK_GB", "1000")
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")


def _student(db, tenant_id, email: str) -> User:
    u = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email=email, display_name=email.split("@")[0],
             role=UserRole.student, tenant_id=tenant_id, source="ad")
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def world(db_session, client, small_cluster):
    db = db_session
    a, b = real_tenant(db, "appr-a"), real_tenant(db, "appr-b")
    course = Course(id=uuid.uuid4(), name="SOC Analyst", tenant_id=a.id, is_published=True)
    db.add(course)
    db.flush()
    for t in (a, b):
        db.add(RollCallSettings(tenant_id=t.id, email_domains=["corp.test"]))
    w = {
        "a": a, "b": b, "course": course,
        "lead": real_user(db, UserRole.instructor, a.id),
        "assistant": real_user(db, UserRole.instructor, a.id),
        "other_instructor": real_user(db, UserRole.instructor, a.id),
        "admin": real_user(db, UserRole.admin, a.id),
        "range_ops": real_user(db, UserRole.range_ops, a.id),
        "observer": real_user(db, UserRole.observer, a.id),
        "student_user": real_user(db, UserRole.student, a.id),
        "b_admin": real_user(db, UserRole.admin, b.id),
        "b_range_ops": real_user(db, UserRole.range_ops, b.id),
        "b_instructor": real_user(db, UserRole.instructor, b.id),
        # On the roll call, and enrolled only (not on the roll call): the latter is not invited.
        "on_roll": _student(db, a.id, "ann@corp.test"),
        "enrolled_only": _student(db, a.id, "eve@corp.test"),
    }
    ensure_enrollment(db, user_id=w["enrolled_only"].id, course_id=course.id, tenant_id=a.id)
    db.flush()
    act_as(w["lead"])
    r = client.post("/classes", json={
        "name": "C201-26-03", "course_id": str(course.id),
        "start_date": str(DAY.date()), "end_date": str((DAY + timedelta(days=4)).date()),
        "assistant_ids": [w["assistant"].id], "observer_ids": [],
    })
    assert r.status_code == 201, r.text
    w["class"] = r.json()
    with patch("app.notifications.get_channel", return_value=Outbox()):  # the onboarding email
        r = client.post(f"/classes/{w['class']['id']}/roll-call",
                        json={"emails": ["ann@corp.test", "newbie@corp.test"]})
    assert r.status_code == 200, r.text
    return w


def _session(start=NINE, hours: float = 3, **extra) -> dict:
    return {
        "name": extra.pop("name", "Blue team lab"),
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=hours)).isoformat(),
        **extra,
    }


def _book_class(client, w, start=NINE, vcpu=40, as_who=None, **extra) -> dict:
    act_as(as_who or w["lead"])
    r = client.post("/schedule/events", json=_session(start, class_id=w["class"]["id"], vcpu_total=vcpu, **extra))
    assert r.status_code == 201, r.text
    return r.json()


def _fill(db, tenant_id, start=NINE, hours: float = 3, vcpu=80, name="someone else") -> ScheduledEvent:
    """A held booking that takes most of the cluster at that time."""
    ev = ScheduledEvent(name=name, state=EventState.scheduled, tenant_id=tenant_id, start_time=start,
                        end_time=start + timedelta(hours=hours), vcpu_total=vcpu)
    db.add(ev)
    db.flush()
    return ev


def _audit(db, event_id: str) -> list[str]:
    return [a.action for a in db.query(AuditLog).filter(AuditLog.resource_id == event_id).order_by(AuditLog.timestamp)]


# ── The state machine ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("start", "to", "ok"),
    [
        (EventState.draft, EventState.pending_approval, True),
        (EventState.pending_approval, EventState.scheduled, True),
        (EventState.pending_approval, EventState.rejected, True),
        (EventState.pending_approval, EventState.cancelled, True),
        (EventState.scheduled, EventState.pending_approval, True),  # an approved session moved
        (EventState.pending_approval, EventState.provisioning, False),  # the clock never builds it
        (EventState.pending_approval, EventState.active, False),
        (EventState.pending_approval, EventState.completed, False),
        (EventState.rejected, EventState.scheduled, False),  # rejected is final
        (EventState.rejected, EventState.pending_approval, False),
        (EventState.rejected, EventState.cancelled, False),
        (EventState.provisioning, EventState.pending_approval, False),  # too late to re-approve
        (EventState.scheduled, EventState.rejected, False),  # only a pending one is rejected
    ],
)
def test_the_approval_states(db_session, start, to, ok):
    t = real_tenant(db_session)
    ev = ScheduledEvent(name="s", state=start, tenant_id=t.id, start_time=NINE, end_time=NINE + timedelta(hours=1))
    db_session.add(ev)
    db_session.flush()
    if ok:
        assert lifecycle.transition(db_session, ev, to) is True
        assert ev.state == to
    else:
        with pytest.raises(HTTPException) as exc:
            lifecycle.transition(db_session, ev, to)
        assert exc.value.status_code == 409


def test_pending_and_rejected_hold_nothing():
    assert EventState.pending_approval not in lifecycle.HOLDING
    assert EventState.rejected not in lifecycle.HOLDING
    assert EventState.rejected in lifecycle.CALLED_OFF


# ── Booking a class session ────────────────────────────────────────────────


def test_a_class_session_waits_for_approval_and_a_plain_booking_does_not(client, db_session, world, outbox):
    ev = _book_class(client, world)
    assert ev["state"] == "pending_approval"
    assert ev["class_id"] == world["class"]["id"]
    assert ev["course_id"] == str(world["course"].id)  # the class's course
    assert ev["instructor_id"] == world["lead"].id
    assert ev["approval"]["requested_by"] == world["lead"].id and ev["approval"]["decided_at"] is None
    assert db_session.query(ClassSession).filter_by(event_id=uuid.UUID(ev["id"])).count() == 1
    assert outbox.sent == []  # nothing is sent before approval
    assert _audit(db_session, ev["id"]) == ["create", "approval_requested"]

    # A booking with no class stays self-service (ADR 0014 §14 is about class bookings).
    plain = client.post("/schedule/events", json=_session(NINE + timedelta(days=1), vcpu_total=10))
    assert plain.status_code == 201 and plain.json()["state"] == "scheduled"
    assert plain.json()["class_id"] is None and plain.json()["approval"] is None


def test_a_class_draft_goes_to_approval_when_scheduled(client, world, outbox):
    ev = _book_class(client, world, draft=True)
    assert ev["state"] == "draft"
    r = client.post(f"/schedule/events/{ev['id']}/schedule")
    assert r.status_code == 200 and r.json()["state"] == "pending_approval"
    # Scheduling it again does not skip the approver.
    assert client.post(f"/schedule/events/{ev['id']}/schedule").json()["state"] == "pending_approval"
    assert outbox.sent == []


@pytest.mark.parametrize(
    ("who", "status"),
    [("lead", 201), ("admin", 201), ("assistant", 403), ("other_instructor", 404), ("b_instructor", 404)],
)
def test_only_the_lead_or_an_admin_books_a_class_session(client, world, who, status):
    act_as(world[who])
    r = client.post("/schedule/events", json=_session(class_id=world["class"]["id"], vcpu_total=5))
    assert r.status_code == status, r.text


def test_a_cancelled_class_takes_no_sessions_and_its_course_is_fixed(client, world):
    act_as(world["lead"])
    other = Course(id=uuid.uuid4(), name="Other", tenant_id=world["a"].id)
    r = client.post("/schedule/events", json=_session(class_id=world["class"]["id"], course_id=str(other.id)))
    assert r.status_code == 422
    client.delete(f"/classes/{world['class']['id']}")
    r = client.post("/schedule/events", json=_session(class_id=world["class"]["id"]))
    assert r.status_code == 409


def test_a_session_keeps_its_class(client, world):
    ev = _book_class(client, world)
    body = _session(class_id=str(uuid.uuid4()), vcpu_total=40)
    assert client.put(f"/schedule/events/{ev['id']}", json=body).status_code == 422


# ── Nothing held, nothing built ────────────────────────────────────────────


def test_pending_holds_no_capacity_and_blocks_no_one(client, db_session, world):
    _book_class(client, world, vcpu=90)
    act_as(world["admin"])
    cap = client.get("/schedule/capacity", params={"start_time": NINE.isoformat(),
                                                   "end_time": (NINE + timedelta(hours=3)).isoformat()}).json()
    assert cap["vcpu_committed"] == 0 and cap["overlapping_events"] == 0
    # Another booking at the same time, same instructor, takes the room.
    act_as(world["lead"])
    r = client.post("/schedule/events", json=_session(vcpu_total=90))
    assert r.status_code == 201 and r.json()["state"] == "scheduled"


def test_the_clock_never_builds_starts_or_reminds_a_pending_session(client, db_session, world, no_real_broker, outbox):
    db = db_session
    tpl = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=world["a"].id)
    db.add(tpl)
    db.flush()
    rng = Range(id=uuid.uuid4(), tenant_id=world["a"].id, name="r", template_id=tpl.id)
    rng.state = RangeState.created
    db.add(rng)
    db.flush()
    ev = _book_class(client, world, range_id=str(rng.id))
    evt = db.get(ScheduledEvent, uuid.UUID(ev["id"]))
    for at in (NINE - timedelta(days=1), NINE - timedelta(minutes=20), NINE + timedelta(minutes=5)):
        res = clock.tick(db, now=at)
        assert res.provisioning == 0 and res.activated == 0 and res.reminders == []
        db.refresh(evt)
        assert evt.state == EventState.pending_approval
    assert no_real_broker.sent == []  # nothing went to the worker
    assert db.get(Range, rng.id).state == RangeState.created
    # Never approved before it ended: it lapses, and leaves the queue.
    res = clock.tick(db, now=NINE + timedelta(hours=4))
    db.refresh(evt)
    assert res.approvals_lapsed == 1 and evt.state == EventState.cancelled
    assert "approval_lapsed" in _audit(db, ev["id"])
    assert outbox.sent == []


# ── Approve ────────────────────────────────────────────────────────────────


def test_approving_sends_the_invites_to_the_roll_call_only(client, db_session, world, outbox):
    ev = _book_class(client, world)
    act_as(world["range_ops"])
    r = client.post(f"/schedule/events/{ev['id']}/approve")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["state"] == "scheduled"
    assert out["approval"]["decided_by"] == world["range_ops"].id
    # The lead teaches; the roll call attends (a linked Student, and one not yet signed in).
    # Course enrolment alone no longer makes someone an attendee.
    assert outbox.invited() == {world["lead"].email, "ann@corp.test", "newbie@corp.test"}
    assert "eve@corp.test" not in outbox.invited()
    assert _audit(db_session, ev["id"])[-1] == "approve"
    # Approving again is a no-op: no second round of invites.
    outbox.clear()
    assert client.post(f"/schedule/events/{ev['id']}/approve").json()["state"] == "scheduled"
    assert outbox.sent == []


def test_reminders_for_an_approved_session_go_to_the_roll_call(client, db_session, world, outbox):
    ev = _book_class(client, world)
    act_as(world["admin"])
    assert client.post(f"/schedule/events/{ev['id']}/approve").status_code == 200
    res = clock.tick(db_session, now=NINE - timedelta(hours=2))
    assert {r.to for r in res.reminders} == {world["lead"].email, "ann@corp.test", "newbie@corp.test"}


def test_approve_rechecks_capacity_and_names_the_next_free_slot(client, db_session, world, outbox):
    ev = _book_class(client, world, vcpu=40)
    _fill(db_session, world["b"].id, vcpu=80)  # booked after the request, by another tenant
    act_as(world["admin"])
    r = client.post(f"/schedule/events/{ev['id']}/approve")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert any(reason.startswith("vCPU: need 40, 20 free") for reason in detail["reasons"])
    nxt = detail["next_free"]
    assert nxt is not None
    # The filler holds 08:30-12:15 with its lead and grace; ours needs 30 min before its start.
    assert datetime.fromisoformat(nxt["start_time"]) == NINE + timedelta(hours=3, minutes=45)
    assert db_session.get(ScheduledEvent, uuid.UUID(ev["id"])).state == EventState.pending_approval
    assert outbox.sent == []


def test_approve_under_warn_approves_with_audited_warnings(client, db_session, world, outbox):
    from app.scheduler.models import SchedulerSetting

    db_session.add(SchedulerSetting(key="overcapacity_policy", value="warn"))
    ev = _book_class(client, world, vcpu=40)
    _fill(db_session, world["b"].id, vcpu=80)
    act_as(world["admin"])
    r = client.post(f"/schedule/events/{ev['id']}/approve")
    assert r.status_code == 200 and r.json()["state"] == "scheduled"
    assert r.json()["warnings"]
    assert _audit(db_session, ev["id"])[-2:] == ["approve", "overcapacity_warning"]


def test_approve_refuses_a_double_booking_whatever_the_policy(client, db_session, world):
    ev = _book_class(client, world, vcpu=5)
    # The lead booked something else over it meanwhile, self-service.
    act_as(world["lead"])
    assert client.post("/schedule/events", json=_session(NINE + timedelta(hours=1), vcpu_total=5)).status_code == 201
    act_as(world["admin"])
    r = client.post(f"/schedule/events/{ev['id']}/approve")
    assert r.status_code == 409
    assert any("Instructor is already teaching" in x for x in r.json()["detail"]["reasons"])
    assert datetime.fromisoformat(r.json()["detail"]["next_free"]["start_time"]) == NINE + timedelta(hours=4)


def test_an_ended_session_cannot_be_approved(client, db_session, world):
    ev = _book_class(client, world)
    evt = db_session.get(ScheduledEvent, uuid.UUID(ev["id"]))
    evt.start_time, evt.end_time = datetime.now(UTC) - timedelta(hours=3), datetime.now(UTC) - timedelta(hours=1)
    db_session.flush()
    act_as(world["admin"])
    assert client.post(f"/schedule/events/{ev['id']}/approve").status_code == 409


def test_a_plain_booking_has_nothing_to_approve(client, world):
    act_as(world["lead"])
    plain = client.post("/schedule/events", json=_session(vcpu_total=5)).json()
    act_as(world["admin"])
    assert client.post(f"/schedule/events/{plain['id']}/approve").status_code == 409


# ── Reject ─────────────────────────────────────────────────────────────────


def test_reject_needs_a_reason_and_is_final(client, db_session, world, outbox):
    ev = _book_class(client, world)
    act_as(world["admin"])
    assert client.post(f"/schedule/events/{ev['id']}/reject", json={"reason": ""}).status_code == 422
    r = client.post(f"/schedule/events/{ev['id']}/reject", json={"reason": "Exercise week: no labs"})
    assert r.status_code == 200
    assert r.json()["state"] == "rejected" and r.json()["approval"]["reason"] == "Exercise week: no labs"
    assert client.post(f"/schedule/events/{ev['id']}/approve").status_code == 409
    act_as(world["lead"])
    assert client.put(f"/schedule/events/{ev['id']}", json=_session(vcpu_total=40)).status_code == 409
    assert client.post(f"/schedule/events/{ev['id']}/cancel").status_code == 409
    assert outbox.sent == []
    rows = db_session.query(AuditLog).filter(AuditLog.resource_id == ev["id"], AuditLog.action == "reject").all()
    assert len(rows) == 1 and "Exercise week" in rows[0].detail


def test_cancelling_a_pending_session_withdraws_nothing(client, world, outbox):
    ev = _book_class(client, world)
    assert client.post(f"/schedule/events/{ev['id']}/cancel").json()["state"] == "cancelled"
    assert outbox.sent == []


# ── Propose, accept, decline ───────────────────────────────────────────────


def test_propose_then_accept_moves_and_approves(client, db_session, world, outbox):
    ev = _book_class(client, world)
    later = NINE + timedelta(days=1)
    act_as(world["range_ops"])
    r = client.post(f"/schedule/events/{ev['id']}/propose",
                    json={"start_time": later.isoformat(), "end_time": (later + timedelta(hours=3)).isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "pending_approval"
    assert datetime.fromisoformat(r.json()["approval"]["proposed_start_time"]) == later
    assert outbox.sent == []

    act_as(world["lead"])
    r = client.post(f"/schedule/events/{ev['id']}/proposal/accept")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["state"] == "scheduled"
    assert datetime.fromisoformat(out["start_time"]) == later
    assert out["approval"]["decided_by"] == world["range_ops"].id  # approved on the proposer's word
    assert out["approval"]["proposed_start_time"] is None
    assert outbox.invited() == {world["lead"].email, "ann@corp.test", "newbie@corp.test"}
    assert _audit(db_session, ev["id"])[-2:] == ["propose", "accept_proposal"]


def test_accept_rechecks_the_proposed_time(client, db_session, world, outbox):
    ev = _book_class(client, world, vcpu=40)
    later = NINE + timedelta(days=1)
    act_as(world["admin"])
    client.post(f"/schedule/events/{ev['id']}/propose",
                json={"start_time": later.isoformat(), "end_time": (later + timedelta(hours=3)).isoformat()})
    _fill(db_session, world["b"].id, start=later, vcpu=80)  # the slot filled up meanwhile
    act_as(world["lead"])
    r = client.post(f"/schedule/events/{ev['id']}/proposal/accept")
    assert r.status_code == 409
    assert r.json()["detail"]["next_free"] is not None
    evt = db_session.get(ScheduledEvent, uuid.UUID(ev["id"]))
    assert evt.state == EventState.pending_approval and evt.start_time.replace(tzinfo=UTC) == NINE
    assert outbox.sent == []


def test_decline_keeps_the_session_pending_at_its_own_time(client, db_session, world):
    ev = _book_class(client, world)
    later = NINE + timedelta(days=2)
    act_as(world["admin"])
    client.post(f"/schedule/events/{ev['id']}/propose",
                json={"start_time": later.isoformat(), "end_time": (later + timedelta(hours=3)).isoformat()})
    act_as(world["lead"])
    r = client.post(f"/schedule/events/{ev['id']}/proposal/decline")
    assert r.status_code == 200
    assert r.json()["state"] == "pending_approval" and r.json()["approval"]["proposed_start_time"] is None
    assert datetime.fromisoformat(r.json()["start_time"]) == NINE
    assert client.post(f"/schedule/events/{ev['id']}/proposal/accept").status_code == 409  # nothing to accept
    assert _audit(db_session, ev["id"])[-2:] == ["propose", "decline_proposal"]


def test_a_proposal_must_fit_and_be_in_the_future(client, db_session, world):
    ev = _book_class(client, world, vcpu=40)
    act_as(world["admin"])
    past = datetime.now(UTC) - timedelta(hours=2)
    r = client.post(f"/schedule/events/{ev['id']}/propose",
                    json={"start_time": past.isoformat(), "end_time": (past + timedelta(hours=1)).isoformat()})
    assert r.status_code == 422
    _fill(db_session, world["b"].id, start=NINE + timedelta(days=3), vcpu=80)
    full = NINE + timedelta(days=3)
    r = client.post(f"/schedule/events/{ev['id']}/propose",
                    json={"start_time": full.isoformat(), "end_time": (full + timedelta(hours=3)).isoformat()})
    assert r.status_code == 409 and r.json()["detail"]["next_free"] is not None


def test_moving_the_session_lets_a_proposal_lapse(client, db_session, world):
    ev = _book_class(client, world)
    later = NINE + timedelta(days=1)
    act_as(world["admin"])
    client.post(f"/schedule/events/{ev['id']}/propose",
                json={"start_time": later.isoformat(), "end_time": (later + timedelta(hours=3)).isoformat()})
    act_as(world["lead"])
    r = client.put(f"/schedule/events/{ev['id']}", json=_session(NINE + timedelta(hours=1), vcpu_total=40))
    assert r.status_code == 200 and r.json()["state"] == "pending_approval"
    assert r.json()["approval"]["proposed_start_time"] is None


def test_moving_an_approved_session_needs_approval_again(client, db_session, world, outbox):
    ev = _book_class(client, world)
    act_as(world["admin"])
    client.post(f"/schedule/events/{ev['id']}/approve")
    outbox.clear()
    act_as(world["lead"])
    # A new name alone keeps the approval (an updated invite).
    r = client.put(f"/schedule/events/{ev['id']}", json=_session(name="Renamed", vcpu_total=40))
    assert r.json()["state"] == "scheduled" and outbox.invited() >= {"ann@corp.test"}
    outbox.clear()
    # A new time: back to the approver, and the invites are withdrawn until then.
    r = client.put(f"/schedule/events/{ev['id']}", json=_session(NINE + timedelta(hours=2), name="Renamed",
                                                                  vcpu_total=40))
    assert r.status_code == 200 and r.json()["state"] == "pending_approval"
    assert outbox.invited("CANCEL") == {world["lead"].email, "ann@corp.test", "newbie@corp.test"}
    assert _audit(db_session, ev["id"])[-1] == "approval_requested"


# ── RBAC and tenancy ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("who", "status"),
    [("admin", 200), ("range_ops", 200), ("lead", 403), ("other_instructor", 403), ("observer", 403),
     ("student_user", 403), ("b_admin", 404), ("b_range_ops", 404)],
)
def test_who_may_approve(client, world, who, status):
    ev = _book_class(client, world)
    act_as(world[who])
    assert client.post(f"/schedule/events/{ev['id']}/approve").status_code == status


@pytest.mark.parametrize(
    ("who", "status"),
    [("admin", 200), ("range_ops", 200), ("lead", 403), ("observer", 403), ("student_user", 403),
     ("b_range_ops", 404)],
)
def test_who_may_reject_and_propose(client, world, who, status):
    ev = _book_class(client, world)
    act_as(world[who])
    assert client.post(f"/schedule/events/{ev['id']}/reject", json={"reason": "no"}).status_code == status
    ev2 = _book_class(client, world, start=NINE + timedelta(days=1))
    act_as(world[who])
    later = NINE + timedelta(days=2)
    r = client.post(f"/schedule/events/{ev2['id']}/propose",
                    json={"start_time": later.isoformat(), "end_time": (later + timedelta(hours=3)).isoformat()})
    assert r.status_code == status


@pytest.mark.parametrize(
    ("who", "status"),
    [("lead", 200), ("admin", 200), ("other_instructor", 403), ("range_ops", 403), ("observer", 403),
     ("b_instructor", 404)],
)
def test_who_may_accept_a_proposal(client, world, who, status):
    ev = _book_class(client, world)
    later = NINE + timedelta(days=1)
    act_as(world["admin"])
    client.post(f"/schedule/events/{ev['id']}/propose",
                json={"start_time": later.isoformat(), "end_time": (later + timedelta(hours=3)).isoformat()})
    act_as(world[who])
    assert client.post(f"/schedule/events/{ev['id']}/proposal/accept").status_code == status


def test_the_queue_is_the_tenants_own_with_fit_and_next_free(client, db_session, world):
    fits = _book_class(client, world, vcpu=10, name="Fits")
    tight = _book_class(client, world, start=NINE + timedelta(days=1), vcpu=40, name="Tight")
    _fill(db_session, world["b"].id, start=NINE + timedelta(days=1), vcpu=80)
    # Another tenant's session awaiting approval: never in this tenant's queue.
    theirs = ScheduledEvent(name="Theirs", state=EventState.pending_approval, tenant_id=world["b"].id,
                            start_time=NINE, end_time=NINE + timedelta(hours=1))
    db_session.add(theirs)
    db_session.flush()
    db_session.add(BookingApproval(event_id=theirs.id, tenant_id=world["b"].id))
    db_session.flush()
    act_as(world["admin"])
    items = client.get("/schedule/approvals").json()["items"]
    assert [i["event"]["id"] for i in items] == [fits["id"], tight["id"]]
    assert items[0]["fit"]["fits"] is True and items[0]["class_name"] == "C201-26-03"
    assert items[1]["fit"]["fits"] is False and items[1]["fit"]["next_free"] is not None
    act_as(world["b_admin"])
    assert [i["event"]["id"] for i in client.get("/schedule/approvals").json()["items"]] == [str(theirs.id)]
    for who, status in (("lead", 403), ("observer", 403), ("student_user", 403), ("range_ops", 200)):
        act_as(world[who])
        assert client.get("/schedule/approvals").status_code == status


def test_next_free_for_a_booking(client, db_session, world):
    ev = _book_class(client, world, vcpu=40)
    _fill(db_session, world["b"].id, vcpu=80)
    act_as(world["lead"])
    r = client.get(f"/schedule/events/{ev['id']}/next-free")
    assert r.status_code == 200
    body = r.json()
    assert body["fits"] is False and body["reasons"]
    assert datetime.fromisoformat(body["next_free"]["start_time"]) == NINE + timedelta(hours=3, minutes=45)
    later = NINE + timedelta(days=1)
    r = client.get(f"/schedule/events/{ev['id']}/next-free", params={"after": later.isoformat()})
    assert r.json()["fits"] is True and datetime.fromisoformat(r.json()["next_free"]["start_time"]) == later
    act_as(world["b_admin"])
    assert client.get(f"/schedule/events/{ev['id']}/next-free").status_code == 404


def test_listing_shows_the_class_and_approval(client, world):
    ev = _book_class(client, world)
    act_as(world["observer"])
    items = client.get("/schedule/events", params={"start": DAY.isoformat(),
                                                   "end": (DAY + timedelta(days=1)).isoformat()}).json()["items"]
    mine = next(i for i in items if i["id"] == ev["id"])
    assert mine["class_id"] == world["class"]["id"] and mine["state"] == "pending_approval"


def test_a_pending_session_gives_its_instructor_no_authority_over_the_course(client, db_session, world):
    from app.class_setup import scope

    _book_class(client, world)
    assert not scope._my_live_bookings(db_session, world["lead"]).all()


# ── Roll-call changes after approval (ADR 0014 §17) ────────────────────────


@pytest.fixture
def seats(monkeypatch):
    """Ten vCPU a seat: #282's per-Student ranges, which this slice leaves to that seam."""
    monkeypatch.setattr(class_sessions, "seat_demand", lambda db, cls, evt: Resources(10, 0, 0))


def _approved(client, world, vcpu=40) -> dict:
    ev = _book_class(client, world, vcpu=vcpu)
    act_as(world["admin"])
    assert client.post(f"/schedule/events/{ev['id']}/approve").status_code == 200
    return ev


def test_with_no_seat_cost_additions_are_always_automatic(client, db_session, world, outbox):
    ev = _approved(client, world, vcpu=95)
    outbox.clear()
    act_as(world["lead"])
    r = client.post(f"/classes/{world['class']['id']}/roll-call", json={"emails": ["late@corp.test"]})
    assert r.status_code == 200 and [e["email"] for e in r.json()["added"]] == ["late@corp.test"]
    assert r.json()["pending_approval"] == []
    assert "late@corp.test" in outbox.invited()  # invited to the approved session
    assert db_session.get(ScheduledEvent, uuid.UUID(ev["id"])).vcpu_total == 95


def test_an_addition_that_fits_is_automatic_and_takes_a_seat(client, db_session, world, outbox, seats):
    ev = _approved(client, world, vcpu=40)
    outbox.clear()
    act_as(world["lead"])
    r = client.post(f"/classes/{world['class']['id']}/roll-call", json={"emails": ["late@corp.test"]})
    assert [e["email"] for e in r.json()["added"]] == ["late@corp.test"]
    assert db_session.get(ScheduledEvent, uuid.UUID(ev["id"])).vcpu_total == 50
    assert "late@corp.test" in outbox.invited()


def test_an_addition_that_does_not_fit_waits_for_an_approver(client, db_session, world, outbox, seats):
    ev = _approved(client, world, vcpu=40)
    _fill(db_session, world["b"].id, vcpu=55)  # 5 vCPU left: no room for a 10-vCPU seat
    outbox.clear()
    act_as(world["lead"])
    r = client.post(f"/classes/{world['class']['id']}/roll-call", json={"emails": ["late@corp.test"]})
    assert r.status_code == 200
    assert r.json()["added"] == []
    assert [p["email"] for p in r.json()["pending_approval"]] == ["late@corp.test"]
    assert "vCPU" in r.json()["pending_approval"][0]["reason"]
    assert outbox.sent == []  # not onboarded, not invited
    entry = db_session.query(RollCallEntry).filter_by(email="late@corp.test").one()
    assert entry.status == "pending" and entry.user_id is None
    assert db_session.get(ScheduledEvent, uuid.UUID(ev["id"])).vcpu_total == 40
    # Not on the roll call: invites and the class's count leave it out.
    from app.classes.models import Class
    from app.classes.service import class_roll_call_emails

    cls = db_session.get(Class, uuid.UUID(world["class"]["id"]))
    assert "late@corp.test" not in class_roll_call_emails(db_session, cls)

    # The approver's list; others do not see it.
    act_as(world["range_ops"])
    pending = client.get("/classes/pending-additions").json()
    assert [p["entry_id"] for p in pending] == [str(entry.id)]
    # Only what a capacity decision needs: no Student named to an approver who need not be on the class.
    assert "email" not in pending[0] and "added_by" not in pending[0]
    assert pending[0]["class_name"] == "C201-26-03" and pending[0]["seats"] == 1 and "vCPU" in pending[0]["reason"]
    assert "late@corp.test" not in client.get("/classes/pending-additions").text
    for who in ("lead", "observer"):
        act_as(world[who])
        assert client.get("/classes/pending-additions").status_code == 403
    act_as(world["b_range_ops"])
    assert client.get("/classes/pending-additions").json() == []
    url = f"/classes/{world['class']['id']}/roll-call/{entry.id}"
    assert client.post(f"{url}/approve").status_code == 404  # another tenant's

    # Still no room: the approver cannot approve it yet.
    act_as(world["range_ops"])
    r = client.post(f"{url}/approve")
    assert r.status_code == 409 and r.json()["detail"]["reasons"]

    # Removing a Student is always allowed, and frees their seat: now it fits.
    ann = db_session.query(RollCallEntry).filter_by(email="ann@corp.test").one()
    act_as(world["lead"])
    assert client.delete(f"/classes/{world['class']['id']}/roll-call/{ann.id}").status_code == 204
    assert "ann@corp.test" in outbox.invited("CANCEL")
    assert db_session.get(ScheduledEvent, uuid.UUID(ev["id"])).vcpu_total == 30
    act_as(world["range_ops"])
    r = client.post(f"{url}/approve")
    assert r.status_code == 200 and r.json()["status"] == "invited"
    assert "late@corp.test" in outbox.invited()
    assert db_session.get(ScheduledEvent, uuid.UUID(ev["id"])).vcpu_total == 40
    actions = [a.action for a in db_session.query(AuditLog).filter(AuditLog.resource_id == world["class"]["id"])]
    assert {"roll_call_add_pending", "roll_call_remove", "roll_call_addition_approved"} <= set(actions)


def test_an_approver_rejects_an_addition(client, db_session, world, seats):
    _approved(client, world, vcpu=40)
    _fill(db_session, world["b"].id, vcpu=55)
    act_as(world["lead"])
    client.post(f"/classes/{world['class']['id']}/roll-call", json={"emails": ["late@corp.test"]})
    entry = db_session.query(RollCallEntry).filter_by(email="late@corp.test").one()
    url = f"/classes/{world['class']['id']}/roll-call/{entry.id}"
    act_as(world["lead"])
    assert client.post(f"{url}/reject", json={"reason": "full"}).status_code == 403
    act_as(world["admin"])
    r = client.post(f"{url}/reject", json={"reason": "The lab is full this week"})
    assert r.status_code == 200 and r.json()["status"] == "removed"
    assert "email" not in r.json() and "pending_reason" not in r.json()
    assert "full" in db_session.get(RollCallEntry, entry.id).pending_reason
    assert client.post(f"{url}/approve").status_code == 409
    rows = db_session.query(AuditLog).filter(AuditLog.action == "roll_call_addition_rejected").all()
    assert len(rows) == 1


def test_before_approval_additions_are_never_held_back(client, db_session, world, seats):
    _book_class(client, world, vcpu=40)  # pending: nothing is held, nothing to fit
    _fill(db_session, world["b"].id, vcpu=95)
    act_as(world["lead"])
    r = client.post(f"/classes/{world['class']['id']}/roll-call", json={"emails": ["late@corp.test"]})
    assert [e["email"] for e in r.json()["added"]] == ["late@corp.test"]


def test_the_approval_row_is_tenant_pinned(db_session, client, world):
    ev = _book_class(client, world)
    row = db_session.get(BookingApproval, uuid.UUID(ev["id"]))
    assert row.tenant_id == world["a"].id
    link = db_session.query(ClassSession).filter_by(event_id=uuid.UUID(ev["id"])).one()
    assert link.tenant_id == world["a"].id


# ── Adversarial review of #281 ─────────────────────────────────────────────


def test_a_plain_booking_of_a_running_class_course_is_refused_to_its_instructors(client, world):
    """Leaving class_id out must not skip the approver (and invite the whole enrolment)."""
    course = str(world["course"].id)
    act_as(world["lead"])
    r = client.post("/schedule/events", json=_session(course_id=course, vcpu_total=5))
    assert r.status_code == 422 and "open class" in r.json()["detail"]
    assert "C201-26-03" not in r.json()["detail"]  # never names a class the caller may not be on
    # A draft holds nothing; scheduling it is refused the same way.
    draft = client.post("/schedule/events", json=_session(course_id=course, vcpu_total=5, draft=True))
    assert draft.status_code == 201
    assert client.post(f"/schedule/events/{draft.json()['id']}/schedule").status_code == 422
    # Outside the class's dates it is an ordinary booking of the course.
    later = NINE + timedelta(days=30)
    assert client.post("/schedule/events", json=_session(later, course_id=course, vcpu_total=5)).status_code == 201
    # An approver may book it plainly.
    act_as(world["admin"])
    assert client.post("/schedule/events", json=_session(course_id=course, vcpu_total=5)).status_code == 201


def test_cancelling_a_class_cancels_its_unstarted_sessions(client, db_session, world, outbox):
    approved = _book_class(client, world, name="Approved")
    pending = _book_class(client, world, start=NINE + timedelta(days=1), name="Pending")
    act_as(world["admin"])
    client.post(f"/schedule/events/{approved['id']}/approve")
    outbox.clear()
    act_as(world["lead"])
    assert client.delete(f"/classes/{world['class']['id']}").status_code == 204
    for ev in (approved, pending):
        assert db_session.get(ScheduledEvent, uuid.UUID(ev["id"])).state == EventState.cancelled
    # Only the approved one was ever sent: its invites are withdrawn.
    assert outbox.invited("CANCEL") == {world["lead"].email, "ann@corp.test", "newbie@corp.test"}
    assert len(outbox.sent) == 3
    res = clock.tick(db_session, now=NINE - timedelta(hours=2))
    assert res.reminders == [] and res.provisioning == 0


def test_approving_again_after_a_move_sends_a_newer_sequence(client, db_session, world, outbox):
    ev = _book_class(client, world)
    act_as(world["admin"])
    client.post(f"/schedule/events/{ev['id']}/approve")
    act_as(world["lead"])
    client.put(f"/schedule/events/{ev['id']}", json=_session(NINE + timedelta(hours=2), vcpu_total=40))
    cancelled_at = db_session.get(ScheduledEvent, uuid.UUID(ev["id"])).sequence
    assert cancelled_at >= 1
    act_as(world["admin"])
    assert client.post(f"/schedule/events/{ev['id']}/approve").status_code == 200
    db_session.expire_all()
    assert db_session.get(ScheduledEvent, uuid.UUID(ev["id"])).sequence > cancelled_at


def test_a_pending_session_makes_nobody_a_participant(client, db_session, world):
    from types import SimpleNamespace

    from app import exercise_participants

    ev = _book_class(client, world)
    evt = db_session.get(ScheduledEvent, uuid.UUID(ev["id"]))
    tpl = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=world["a"].id)
    db_session.add(tpl)
    db_session.flush()
    rng = Range(id=uuid.uuid4(), tenant_id=world["a"].id, name="r", template_id=tpl.id)
    db_session.add(rng)
    db_session.flush()
    evt.range_id = rng.id
    db_session.flush()
    ex = SimpleNamespace(id=uuid.uuid4(), tenant_id=world["a"].id, range_id=rng.id)
    assert exercise_participants._bookings(db_session, ex).all() == []
    act_as(world["admin"])
    assert client.post(f"/schedule/events/{ev['id']}/approve").status_code == 200
    assert [tuple(r) for r in exercise_participants._bookings(db_session, ex).all()] == [(world["course"].id,)]


def test_a_students_own_sessions_follow_the_roll_call(client, db_session, world):
    from app.auth import CurrentUser
    from app.scheduler import feed, ics, service

    ev = _book_class(client, world)

    def mine(u: User) -> list[str]:
        who = CurrentUser(id=str(u.id), email=u.email, display_name="s", role=UserRole.student,
                          tenant_id=str(world["a"].id), keycloak_id="kc")
        return [str(e.id) for e in service.my_sessions(db_session, who)]

    assert mine(world["on_roll"]) == []  # awaiting approval: nobody's yet
    act_as(world["admin"])
    client.post(f"/schedule/events/{ev['id']}/approve")
    assert mine(world["on_roll"]) == [ev["id"]]
    assert mine(world["enrolled_only"]) == []  # enrolled on the course, not on the roll call
    uid = ics.booking_uid(uuid.UUID(ev["id"]))
    assert uid in feed.render(db_session, world["on_roll"]).replace("\r\n ", "")
    assert uid not in feed.render(db_session, world["enrolled_only"]).replace("\r\n ", "")


# ── Second review of PR #297 ───────────────────────────────────────────────


def _class_body(w, **over) -> dict:
    body = {"name": "C201-26-03", "course_id": str(w["course"].id), "start_date": str(DAY.date()),
            "end_date": str((DAY + timedelta(days=4)).date()), "assistant_ids": [w["assistant"].id],
            "observer_ids": []}
    body.update(over)
    return body


def test_1_a_class_changing_course_tells_the_old_course_nothing(client, db_session, world, outbox):
    act_as(world["lead"])
    cls = client.post("/classes", json=_class_body(world, name="C2", assistant_ids=[])).json()
    with patch("app.notifications.get_channel", return_value=Outbox()):
        client.post(f"/classes/{cls['id']}/roll-call", json={"emails": ["solo@corp.test"]})
    ev = client.post("/schedule/events", json=_session(class_id=cls["id"], vcpu_total=10)).json()
    act_as(world["admin"])
    assert client.post(f"/schedule/events/{ev['id']}/approve").status_code == 200
    other = Course(id=uuid.uuid4(), name="Other", tenant_id=world["a"].id, is_published=True)
    db_session.add(other)
    db_session.flush()
    act_as(world["lead"])
    assert client.put(f"/classes/{cls['id']}", json=_class_body(
        world, name="C2", assistant_ids=[], course_id=str(other.id))).status_code == 200
    outbox.clear()
    assert client.put(f"/schedule/events/{ev['id']}", json=_session(name="renamed", vcpu_total=10)).status_code == 200
    assert "eve@corp.test" not in outbox.invited("CANCEL")  # enrolled on the old course only
    assert outbox.invited("CANCEL") == set()


def test_2_a_student_put_back_gets_a_newer_request_than_their_cancel(client, db_session, world, outbox):
    ev = _approved(client, world)
    entry = db_session.query(RollCallEntry).filter(RollCallEntry.email == "newbie@corp.test").one()
    act_as(world["lead"])
    assert client.delete(f"/classes/{world['class']['id']}/roll-call/{entry.id}").status_code == 204
    cancelled_at = db_session.get(ScheduledEvent, uuid.UUID(ev["id"])).sequence
    outbox.clear()
    assert client.post(f"/classes/{world['class']['id']}/roll-call", json={"emails": ["newbie@corp.test"]}).status_code == 200
    assert "newbie@corp.test" in outbox.invited("REQUEST")
    assert db_session.get(ScheduledEvent, uuid.UUID(ev["id"])).sequence > cancelled_at


def test_4_session_control_follows_the_class_after_a_handover(client, db_session, world):
    from app.class_setup import scope

    ev = _book_class(client, world)
    act_as(world["lead"])
    assert client.put(f"/classes/{world['class']['id']}", json=_class_body(
        world, lead_instructor_id=world["other_instructor"].id)).status_code == 200
    evt = db_session.get(ScheduledEvent, uuid.UUID(ev["id"]))
    assert str(evt.instructor_id) == world["other_instructor"].id  # the new lead teaches it
    # The old lead has no say over it, nor class_setup authority through it; the new lead does.
    assert client.put(f"/schedule/events/{ev['id']}", json=_session(vcpu_total=40)).status_code == 403
    assert not scope._my_live_bookings(db_session, world["lead"]).all()
    act_as(world["other_instructor"])
    assert client.put(f"/schedule/events/{ev['id']}", json=_session(name="New lead's", vcpu_total=40)).status_code == 200
    # An assistant is not the class's editor, even when named the session's instructor.
    r = client.put(f"/schedule/events/{ev['id']}",
                   json=_session(vcpu_total=40, instructor_id=world["assistant"].id))
    assert r.status_code == 200 and r.json()["instructor_id"] == world["assistant"].id
    act_as(world["assistant"])
    assert client.post(f"/schedule/events/{ev['id']}/cancel").status_code == 403


def test_4_a_class_session_is_taught_by_the_lead_or_an_assistant(client, world):
    act_as(world["lead"])
    body = _session(class_id=world["class"]["id"], vcpu_total=5, instructor_id=world["other_instructor"].id)
    assert client.post("/schedule/events", json=body).status_code == 422
    body["instructor_id"] = world["assistant"].id
    assert client.post("/schedule/events", json=body).status_code == 201


def test_5_a_draft_class_session_makes_nobody_a_participant(client, db_session, world):
    from types import SimpleNamespace

    from app import exercise_participants

    ev = _book_class(client, world, draft=True)
    tpl = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=world["a"].id)
    db_session.add(tpl)
    db_session.flush()
    rng = Range(id=uuid.uuid4(), tenant_id=world["a"].id, name="r", template_id=tpl.id)
    db_session.add(rng)
    db_session.flush()
    db_session.get(ScheduledEvent, uuid.UUID(ev["id"])).range_id = rng.id
    db_session.flush()
    ex = SimpleNamespace(id=uuid.uuid4(), tenant_id=world["a"].id, range_id=rng.id)
    assert exercise_participants._bookings(db_session, ex).all() == []


def test_6_additions_take_the_booking_lock_only_when_a_seat_costs_something(client, world, monkeypatch, outbox):
    from app.scheduler import service as scheduler_service

    _approved(client, world)
    taken = []
    real = scheduler_service.serialize_bookings
    monkeypatch.setattr(scheduler_service, "serialize_bookings", lambda db: (taken.append(1), real(db)))
    act_as(world["lead"])
    assert client.post(f"/classes/{world['class']['id']}/roll-call", json={"emails": ["a1@corp.test"]}).status_code == 200
    assert taken == []
    monkeypatch.setattr(class_sessions, "seat_demand", lambda db, cls, evt: Resources(1, 0, 0))
    assert client.post(f"/classes/{world['class']['id']}/roll-call", json={"emails": ["a2@corp.test"]}).status_code == 200
    assert taken


def test_6_a_lost_lock_race_is_409_not_500(client, world, monkeypatch):
    from app.classes import service as class_service
    from sqlalchemy.exc import OperationalError

    class DeadlockError(Exception):
        sqlstate = "40P01"

    def boom(*a, **k):
        raise OperationalError("INSERT ...", {}, DeadlockError())

    monkeypatch.setattr(class_service, "add_to_roll_call", boom)
    act_as(world["lead"])
    r = client.post(f"/classes/{world['class']['id']}/roll-call", json={"emails": ["x@corp.test"]})
    assert r.status_code == 409


def _pending_addition(client, db_session, world) -> RollCallEntry:
    _approved(client, world, vcpu=40)
    _fill(db_session, world["b"].id, vcpu=55)
    act_as(world["lead"])
    with patch("app.notifications.get_channel", return_value=Outbox()):
        client.post(f"/classes/{world['class']['id']}/roll-call", json={"emails": ["late@corp.test"]})
    entry = db_session.query(RollCallEntry).filter_by(email="late@corp.test").one()
    assert entry.status == "pending"
    db_session.query(ScheduledEvent).filter(ScheduledEvent.name == "someone else").delete()
    db_session.flush()
    return entry


def test_8_approving_an_addition_rechecks_the_domain_allow_list(client, db_session, world, seats):
    entry = _pending_addition(client, db_session, world)
    db_session.get(RollCallSettings, world["a"].id).email_domains = ["other.test"]
    db_session.flush()
    act_as(world["admin"])
    r = client.post(f"/classes/{world['class']['id']}/roll-call/{entry.id}/approve")
    assert r.status_code == 422 and "corp.test" in r.json()["detail"]


def test_8_approving_an_addition_counts_against_the_daily_email_budget(client, db_session, world, seats, monkeypatch,
                                                                        outbox):
    from app.classes import service as class_service

    entry = _pending_addition(client, db_session, world)
    monkeypatch.setattr(class_service, "EMAILS_PER_INSTRUCTOR_PER_DAY", 0)
    outbox.clear()
    act_as(world["admin"])
    r = client.post(f"/classes/{world['class']['id']}/roll-call/{entry.id}/approve")
    assert r.status_code == 200 and r.json()["emailed"] is False and r.json()["not_emailed"]
    assert not [m for m in outbox.sent if m["method"] is None]  # no onboarding email
    assert db_session.get(RollCallEntry, entry.id).onboarding_sent_at is None


def test_9_a_proposal_stays_inside_the_class_dates_and_out_of_maintenance(client, db_session, world):
    from app.maintenance_windows.models import MaintenanceWindow

    ev = _book_class(client, world)
    act_as(world["admin"])
    late = DAY + timedelta(days=10, hours=9)
    r = client.post(f"/schedule/events/{ev['id']}/propose",
                    json={"start_time": late.isoformat(), "end_time": (late + timedelta(hours=3)).isoformat()})
    assert r.status_code == 409 and any("class's dates" in x for x in r.json()["detail"]["reasons"])
    day2 = NINE + timedelta(days=1)
    db_session.add(MaintenanceWindow(tenant_id=world["a"].id, reason="patching", scope="all",
                                     starts_at=day2 - timedelta(hours=1), ends_at=day2 + timedelta(hours=4)))
    db_session.flush()
    r = client.post(f"/schedule/events/{ev['id']}/propose",
                    json={"start_time": day2.isoformat(), "end_time": (day2 + timedelta(hours=3)).isoformat()})
    assert r.status_code == 409 and any("maintenance" in x for x in r.json()["detail"]["reasons"])
    # The next free slot steps past the window: its build starts once the window ends.
    assert datetime.fromisoformat(r.json()["detail"]["next_free"]["start_time"]) == day2 + timedelta(hours=4, minutes=30)


def test_9_next_free_never_leaves_the_class_dates(client, db_session, world):
    ev = _book_class(client, world, vcpu=40)
    _fill(db_session, world["b"].id, start=DAY, hours=24 * 6, vcpu=80)  # the whole class, and past it
    act_as(world["lead"])
    body = client.get(f"/schedule/events/{ev['id']}/next-free").json()
    assert body["fits"] is False and body["next_free"] is None


# ── Third review of PR #297: sessions follow the class's teachers ──────────


def _taught(db, u) -> set:
    from app.class_setup import scope

    return scope.taught_course_ids(db, u)


def test_r3_an_assistant_taken_off_loses_the_sessions_they_taught(client, db_session, world, outbox):
    ev = _book_class(client, world, instructor_id=world["assistant"].id)
    act_as(world["admin"])
    assert client.post(f"/schedule/events/{ev['id']}/approve").status_code == 200
    assert world["course"].id in _taught(db_session, world["assistant"])
    outbox.clear()
    act_as(world["lead"])
    assert client.put(f"/classes/{world['class']['id']}", json=_class_body(world, assistant_ids=[])).status_code == 200
    evt = db_session.get(ScheduledEvent, uuid.UUID(ev["id"]))
    assert str(evt.instructor_id) == world["lead"].id and evt.state == EventState.scheduled
    assert world["course"].id not in _taught(db_session, world["assistant"])
    assert world["assistant"].email in outbox.invited("CANCEL") and world["lead"].email in outbox.invited("REQUEST")
    act_as(world["assistant"])
    assert ev["id"] not in [s["id"] for s in client.get("/schedule/mine").json()]


def test_r3_an_old_lead_kept_as_assistant_then_taken_off_loses_them(client, db_session, world):
    ev = _book_class(client, world)
    act_as(world["admin"])
    client.post(f"/schedule/events/{ev['id']}/approve")
    act_as(world["lead"])
    assert client.put(f"/classes/{world['class']['id']}", json=_class_body(
        world, lead_instructor_id=world["other_instructor"].id,
        assistant_ids=[world["assistant"].id, world["lead"].id])).status_code == 200
    evt = db_session.get(ScheduledEvent, uuid.UUID(ev["id"]))
    assert str(evt.instructor_id) == world["lead"].id  # still one of its teachers: keeps it
    act_as(world["other_instructor"])
    assert client.put(f"/classes/{world['class']['id']}", json=_class_body(
        world, lead_instructor_id=world["other_instructor"].id)).status_code == 200
    db_session.refresh(evt)
    assert str(evt.instructor_id) == world["other_instructor"].id
    assert world["course"].id not in _taught(db_session, world["lead"])


def test_r3_a_handover_to_a_busy_lead_sends_the_session_back_for_approval(client, db_session, world, outbox):
    ev = _book_class(client, world)
    act_as(world["admin"])
    assert client.post(f"/schedule/events/{ev['id']}/approve").status_code == 200
    busy = ScheduledEvent(name="New lead busy", state=EventState.scheduled, tenant_id=world["a"].id,
                          instructor_id=uuid.UUID(world["other_instructor"].id), start_time=NINE,
                          end_time=NINE + timedelta(hours=3), vcpu_total=1)
    db_session.add(busy)
    db_session.flush()
    sent_at = db_session.get(ScheduledEvent, uuid.UUID(ev["id"])).sequence
    outbox.clear()
    act_as(world["lead"])
    r = client.put(f"/classes/{world['class']['id']}", json=_class_body(
        world, lead_instructor_id=world["other_instructor"].id))
    assert r.status_code == 200  # the class update itself is not refused
    evt = db_session.get(ScheduledEvent, uuid.UUID(ev["id"]))
    assert evt.state == EventState.pending_approval and str(evt.instructor_id) == world["other_instructor"].id
    assert evt.sequence > sent_at
    # Its invites are withdrawn (to the old lead and the roll call); nothing new is sent.
    assert {world["lead"].email, "ann@corp.test", "newbie@corp.test"} <= outbox.invited("CANCEL")
    assert outbox.invited("REQUEST") == set()
    actions = _audit(db_session, ev["id"])
    assert "approval_requested" in actions and actions.count("update") >= 1
