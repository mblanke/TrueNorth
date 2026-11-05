"""Regression oracle for the confirmed audit findings of #443 (card TN10X-AUDIT-tests-classes).

One test per finding that is still present at base 280a838b. Each asserts the *correct*
behaviour, so each FAILS at the base commit; the failure is the oracle the later fix must
turn green. Findings (file:line, title):

1. control-plane/api/app/classes/router.py:349 — Lead instructor re-dates an open class
   after approval; the class hold stretches to the new end_date with no re-approval.
2. control-plane/api/app/classes/router.py:384 — Cancel racing Close-out overwrites a
   closed class to cancelled.
3. control-plane/api/app/classes/service.py:476 — Reject of a pending roll-call addition is
   unguarded: racing an Approve it removes an admitted Student without freeing the seat the
   approve took.
4. control-plane/api/app/classes/service.py:150 — A class's course can be changed after its
   sessions were booked when the roll call is invited-only.
5. control-plane/api/app/classes/access.py:35 — A former assistant demoted to Student keeps
   the class in GET /classes.
6. control-plane/api/app/classes/onboarding.py:100 — Admission uses the UTC calendar day
   for the class's last day; a last-evening sign-in at a western site is refused.

Finding already fixed at this base — no failing test here, and none allowed:
- "A console no longer closes when its sign-in token expires" was fixed by #442 (commit
  54dca7af), which shipped its own regression coverage in tests/api/test_console_relay.py.
  That file passes at this base (verified with the required-check interpreter).

The two race findings (2 and 3) cannot spawn a second in-process writer: the test engine is
one pooled in-memory SQLite connection. Each replays the concurrent transaction's
*committed* write with a Core ``UPDATE ...`` executed behind the ORM's back
(``synchronize_session=False`` leaves the identity map stale, exactly as a second
connection would) while the request or service call is in flight — the deterministic form
of the interleaving, without mocking the code under test.

Every test is marked ``audit443`` so the batch is easy to find: ``pytest -m audit443``.

Per the integrator's decision each failing test carries ``xfail(strict=True)`` naming its
finding, so the suite is green at the base (6 xfailed) while the failure stays the oracle:
the later fix turns its test into an XPASS, which strict mode surfaces as an error until
the marker is dropped. Verify an oracle still bites with ``--runxfail``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from _shared import act_as, real_tenant, real_user
from app.classes import service as class_service
from app.classes import sessions as class_sessions
from app.classes.models import Class, ClassStatus, RollCallEntry, RollCallSettings
from app.models import Course, Template, UserRole
from app.scheduler.capacity import Resources
from app.scheduler.models import EventState, ScheduledEvent
from fastapi import HTTPException
from test_classes_278 import _identity, outbox, sign_in  # noqa: F401 -- fixtures, used by name

# The imported fixture objects are only ever requested by name (as fixture parameters),
# which the gate's breakage rules would read as an unused import being redefined; keep
# the binding genuinely used here.
_DONOR_FIXTURES = (outbox, sign_in)

DAY = (datetime.now(UTC) + timedelta(days=40)).replace(hour=0, minute=0, second=0, microsecond=0)
MON = DAY + timedelta(hours=9)

# One Student's range: 2 VMs, 4 vCPU, 8 GB RAM, 100 GB disk; the shared exercise range:
# 3 VMs, 12 vCPU (as tests/scheduler/test_class_session_ranges.py).
SEAT_YAML = """
nodes:
  - id: kali
    os: kali
    specs: {cores: 2, memory_mb: 4096, disk_gb: 40}
  - id: web01
    os: ubuntu-2404
    specs: {cores: 2, memory_mb: 4096, disk_gb: 60}
accounts:
  - {machine: web01, username: analyst, generate: true, student_visible: true}
"""
SHARED_YAML = """
nodes:
  - id: dc01
    os: win2022
    count: 3
    specs: {cores: 4, memory_mb: 8192, disk_gb: 80}
"""


@pytest.fixture
def cluster(monkeypatch):
    """100 vCPU, 400 GB RAM, 4000 GB disk, no overhead (as the scheduler donors)."""
    monkeypatch.setenv("CLUSTER_TOTAL_VCPU", "100")
    monkeypatch.setenv("CLUSTER_TOTAL_RAM_MB", str(400 * 1024))
    monkeypatch.setenv("CLUSTER_TOTAL_DISK_GB", "4000")
    monkeypatch.setenv("CLUSTER_OVERHEAD_PCT", "0")


# `outbox` and `sign_in` above are the donors' fixtures (tests/api/test_classes_278.py),
# imported by module path — tests/api has no __init__.py, so pytest's sys.path insertion
# resolves the module; never `from conftest import`. pytest registers them as this
# module's fixtures under their own names, so tests request them directly.


@pytest.fixture
def world(db_session, client, outbox, cluster):
    """Tenant A: two published courses, two range templates with real demand, staff, and an
    open class led by ``lead`` with ``assistant`` on it."""
    db = db_session
    a, b = real_tenant(db, "aud443-a"), real_tenant(db, "aud443-b")
    course = Course(id=uuid.uuid4(), name="SOC Analyst", tenant_id=a.id, is_published=True)
    other_course = Course(id=uuid.uuid4(), name="Network Defence", tenant_id=a.id, is_published=True)
    seat_tpl = Template(id=uuid.uuid4(), name="Analyst seat", yaml=SEAT_YAML, tenant_id=a.id)
    shared_tpl = Template(id=uuid.uuid4(), name="Exercise range", yaml=SHARED_YAML, tenant_id=a.id)
    db.add_all([course, other_course, seat_tpl, shared_tpl])
    db.flush()
    db.add(RollCallSettings(tenant_id=a.id, email_domains=["corp.test"]))
    w = {
        "a": a, "b": b, "course": course, "other_course": other_course,
        "seat_tpl": seat_tpl, "shared_tpl": shared_tpl,
        "lead": real_user(db, UserRole.instructor, a.id),
        "assistant": real_user(db, UserRole.instructor, a.id),
        "admin": real_user(db, UserRole.admin, a.id),
    }
    act_as(w["lead"])
    r = client.post("/classes", json=_class_body(w, assistant_ids=[w["assistant"].id]))
    assert r.status_code == 201, r.text
    w["class"] = r.json()
    return w


def _class_body(w, **over):
    body = {
        "name": "C201-26-44",
        "course_id": str(w["course"].id),
        "start_date": str(DAY.date()),
        "end_date": str((DAY + timedelta(days=4)).date()),
        "assistant_ids": [],
        "observer_ids": [],
    }
    body.update(over)
    return body


def _book(client, w, start=MON, hours=3, template="shared_tpl", **extra) -> dict:
    act_as(w["lead"])
    body = {
        "name": "Lab day", "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=hours)).isoformat(), "class_id": w["class"]["id"],
    }
    if template:
        body["template_id"] = str(w[template].id)
    body.update(extra)
    r = client.post("/schedule/events", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _approve(client, w, ev) -> dict:
    act_as(w["admin"])
    r = client.post(f"/schedule/events/{ev['id']}/approve")
    assert r.status_code == 200, r.text
    return r.json()


def _on_roll_call(client, w, *emails):
    act_as(w["lead"])
    r = client.post(f"/classes/{w['class']['id']}/roll-call", json={"emails": list(emails)})
    assert r.status_code == 200, r.text
    return r.json()["added"]


def _fill(db, tenant_id, start=MON, hours: float = 3, vcpu=80, name="someone else") -> ScheduledEvent:
    """A held booking that takes most of the cluster at that time (as the donors)."""
    ev = ScheduledEvent(name=name, state=EventState.scheduled, tenant_id=tenant_id, start_time=start,
                        end_time=start + timedelta(hours=hours), vcpu_total=vcpu)
    db.add(ev)
    db.flush()
    return ev


# ── 1. Re-dating stretches the hold with no re-approval ───────────────────


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Lead instructor re-dates an open class "
                                        "after approval; the class hold stretches to the new "
                                        "end_date with no re-approval (classes/router.py:349)")
def test_f1_redating_an_approved_class_may_not_stretch_its_hold(client, world):
    """classes/router.py:349 — Lead instructor re-dates an open class after approval.

    PUT /classes/{id} accepts a new end_date with no re-approval, and the class hold is
    sized from the new date (holds.py:114 ``last_day_end(cls.end_date)`` + teardown grace;
    capacity._held consumes holds cluster-wide): capacity far beyond the approved sessions
    silently disappears for everyone. Correct behaviour: the far window stays free — the
    PUT is refused, or the sessions go back for approval and the hold ends with them.
    """
    ev = _book(client, world)  # collective session sized by the shared template (12 vCPU)
    _approve(client, world, ev)
    far = DAY + timedelta(days=100, hours=9)  # long after the class's approved plan
    probe = {"start_time": far.isoformat(), "end_time": (far + timedelta(hours=1)).isoformat()}
    act_as(world["admin"])
    assert client.get("/schedule/capacity", params=probe).json()["overlapping_events"] == 0
    act_as(world["lead"])
    extended = _class_body(world, end_date=str((DAY + timedelta(days=200)).date()))
    r = client.put(f"/classes/{world['class']['id']}", json=extended)
    # Only a legitimate outcome may silence the oracle: the re-dating lands (200) or is
    # refused (409) — any other break in PUT /classes must keep this test red.
    assert r.status_code in (200, 409), r.text
    act_as(world["admin"])
    held = client.get("/schedule/capacity", params=probe).json()
    assert held["overlapping_events"] == 0, (
        "the class hold must not stretch past the approved plan without re-approval"
    )


# ── 2. Cancel racing Close-out overwrites closed → cancelled ──────────────


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Cancel racing Close-out overwrites a "
                                        "closed class to cancelled (classes/router.py:384)")
def test_f2_cancel_may_not_overwrite_a_class_closed_concurrently(client, world, db_session, monkeypatch):
    """classes/router.py:384 — Cancel racing Close-out overwrites a closed class.

    cancel_class reads ``cls.status`` from the identity map without a lock; closeout.close()
    locks the row FOR UPDATE and commits ``closed`` in between; cancel's unconditional ORM
    UPDATE then lands after it and flips closed → cancelled — results kept, class hidden,
    unrecoverable. Replayed here by applying the concurrent close's committed write behind
    the ORM's back while the cancel request is in flight (the hook ``service.audit`` runs
    after cancel's stale read, before its commit). Correct behaviour: the class stays
    closed; the cancel must not overwrite it.

    Note for the fixer: this replay certifies a compare-and-set-style fix (as
    approve_addition's, service.py:450) — a hook that fires only after cancel's reads can
    not let a row-lock-at-read or re-read fix pass here, though both are correct in
    production; rework this hook if that is the fix chosen.
    """
    cid = uuid.UUID(world["class"]["id"])
    real_audit = class_service.audit

    def close_lands_while_cancel_is_in_flight(db, *args, **kwargs):
        db.query(Class).filter(Class.id == cid).update(
            {Class.status: ClassStatus.closed.value}, synchronize_session=False
        )
        return real_audit(db, *args, **kwargs)

    monkeypatch.setattr(class_service, "audit", close_lands_while_cancel_is_in_flight)
    act_as(world["admin"])
    client.delete(f"/classes/{cid}")
    db_session.expire_all()
    assert db_session.get(Class, cid).status == ClassStatus.closed.value, (
        "a cancel that raced the close-out must not overwrite closed with cancelled"
    )


# ── 3. Reject racing Approve removes an admitted Student ──────────────────


@pytest.fixture
def seats(monkeypatch):
    """One admitted Student draws 10 vCPU (as tests/scheduler/test_booking_approval.py)."""
    monkeypatch.setattr(class_sessions, "seat_demand", lambda db, cls, evt: Resources(10, 0, 0))


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Reject of a pending roll-call addition is "
                                        "unguarded: racing an Approve it removes an admitted "
                                        "Student without freeing the seat (classes/service.py:476)")
def test_f3_reject_may_not_remove_a_student_approved_concurrently(client, world, db_session, seats):
    """classes/service.py:476 — Reject of a pending roll-call addition is unguarded.

    approve_addition claims the entry with a CAS UPDATE (409 when it lost the race) and
    takes the seat; reject_addition only checks the stale ORM status and then writes
    ``removed`` unconditionally — racing an Approve it removes an admitted Student and
    never frees the seat the approve took. remove_from_roll_call, its guarded twin, does
    both. Replayed here: the approve's committed write (pending → invited) lands behind
    the ORM's back, then the reject runs on the stale entry. Correct behaviour: the reject
    is refused with 409 and the Student stays admitted.
    """
    # An approved session with no template: 40 vCPU (as the donor). Before approval
    # additions are never held back, so the session must be approved to race over.
    _approve(client, world, _book(client, world, template="", vcpu_total=40))
    _fill(db_session, world["b"].id, vcpu=55)  # another tenant takes most of the cluster
    _on_roll_call(client, world, "late@corp.test")  # 40 + 55 + 10 > 100 → held for approval
    entry = db_session.query(RollCallEntry).filter_by(email="late@corp.test").one()
    assert entry.status == "pending"
    # The filler goes before the reject runs, so a fixed reject's 409 can only be the
    # race guard, not a capacity refusal.
    db_session.query(ScheduledEvent).filter(ScheduledEvent.name == "someone else").delete()
    db_session.flush()
    # The concurrent Approve commits: pending → invited (its CAS) and the seat is taken.
    db_session.query(RollCallEntry).filter(RollCallEntry.id == entry.id).update(
        {RollCallEntry.status: "invited"}, synchronize_session=False
    )
    cls = db_session.get(Class, uuid.UUID(world["class"]["id"]))
    with pytest.raises(HTTPException) as exc:
        class_service.reject_addition(db_session, cls, entry, world["admin"], "duplicate request")
    assert exc.value.status_code == 409
    db_session.flush()
    db_session.expire_all()
    assert db_session.get(RollCallEntry, entry.id).status == "invited", (
        "a reject that raced the approve must not remove the admitted Student"
    )


# ── 4. Course switched after the sessions were booked ─────────────────────


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: A class's course can be changed after its "
                                        "sessions were booked; the sessions keep the old "
                                        "course_id (classes/service.py:150)")
def test_f4_a_booked_class_may_not_switch_course_behind_its_sessions(client, world, db_session):
    """classes/service.py:150 — A class's course changes after its sessions were booked.

    service.apply refuses a course change only when the class has linked Students
    (``_has_linked_students``); with an invited-only roll call the class switches course
    even though its session is already booked and approved — ScheduledEvent.course_id
    stays on the old course and nothing realigns it. Correct behaviour: the class's course
    and its booked sessions' course stay in step (the change is refused, or the sessions
    follow).
    """
    ev = _book(client, world)
    _approve(client, world, ev)
    _on_roll_call(client, world, "joe.bloggs@corp.test")  # invited-only: no linked account
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
        "the class's course and its booked session's course must stay in step"
    )


# ── 5. A demoted assistant keeps the class in the list ────────────────────


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: A former assistant/observer demoted to "
                                        "Student keeps the class in GET /classes "
                                        "(classes/access.py:35)")
def test_f5_a_demoted_assistant_no_longer_sees_the_class_in_the_list(client, world):
    """classes/access.py:35 — A former assistant demoted to Student keeps the class.

    visible()'s ClassStaff branch matches any ClassStaff row and ignores the caller's
    current role, while role_from refuses staff standing to a Student: the detail call
    404s, but GET /classes still names the class with its course, dates, lead, assistants,
    observers and roll_call_count, and ``my_role: null``. Correct behaviour: the demoted
    Student is not listed the class either.
    """
    demoted = world["assistant"].model_copy(update={"role": UserRole.student})
    act_as(demoted)
    listed = [c["id"] for c in client.get("/classes").json()]
    assert world["class"]["id"] not in listed, (
        "a Student who is no longer on the class staff must not see it in the list"
    )
    assert client.get(f"/classes/{world['class']['id']}").status_code == 404


# ── 6. Admission gates on the UTC day, not the site day ───────────────────


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: Admission uses the UTC calendar day for "
                                        "the class's last day while the rest of the area uses "
                                        "the site zone (classes/onboarding.py:100)")
def test_f6_admission_keeps_the_site_day_not_the_utc_day(client, world, db_session, sign_in, monkeypatch):
    """classes/onboarding.py:100 — Admission uses the UTC calendar day for the last day.

    matching_entries filters ``Class.end_date >= datetime.now(UTC).date()`` with no slack,
    while the rest of the area (clock.py) works in SITE_TIMEZONE: at a western site, on the
    class's last evening — site date still the end_date, UTC date already the next day —
    the roll-call sign-in is refused. Correct behaviour: the Student is admitted on the
    class's last site day.
    """
    from app.classes import onboarding

    monkeypatch.setenv("SITE_TIMEZONE", "America/Toronto")
    # 2026-06-01 00:30 UTC = 2026-05-31 20:30 in Toronto: the site's last evening of the
    # class. Only the admission gate's clock is frozen; everything else runs for real.
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: A003 - the datetime API
            return datetime(2026, 6, 1, 0, 30, tzinfo=UTC)

    monkeypatch.setattr(onboarding, "datetime", FrozenDatetime)
    cls = db_session.get(Class, uuid.UUID(world["class"]["id"]))
    cls.start_date, cls.end_date = date(2026, 5, 27), date(2026, 5, 31)
    db_session.flush()
    _on_roll_call(client, world, "joe.bloggs@corp.test")
    body = sign_in(_identity())
    assert body["status"] == "registered", (
        "on the class's last site day the sign-in must still admit, not refuse"
    )
