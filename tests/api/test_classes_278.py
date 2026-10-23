"""Classes 1: the Class record, its roll call and roll-call onboarding (ADR 0014; #278, #272).

The rules under test:

- RBAC per role: instructors and admins create classes, in their own tenant only; only the
  lead or an admin changes a class or its roll call; assistants and observers read the roll
  call; a Student on it sees the class but not the roll call; anyone else is 404, and so is
  every other tenant.
- Roll call by paste or CSV. An existing Student of the tenant is linked and enrolled at
  once (#272); a staff account is refused; someone with no account is emailed to onboard.
- Roll call is approval: a verified AD email on exactly one tenant's roll call is admitted
  on first sign-in as a Student of that tenant, enrolled on the course, audited. No match,
  an ambiguous match, an unverified or non-AD email, a staff address, a removed entry or a
  cancelled class all fall back to Request access, with nothing created.
- One reminder the day before, to Students not yet signed in.
- The invite list is the roll call, tenant-pinned: a Student who moved tenant drops off.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from _shared import act_as, real_tenant, real_user
from app.auth import TokenPayload, get_token_identity
from app.classes import clock, service
from app.classes.models import Class, RollCallEntry
from app.main import app as fastapi_app
from app.models import AuditLog, Course, Enrollment, RegistrationRequest, RegistrationStatus, User, UserRole

TODAY = datetime.now(UTC).date()


class Outbox:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, to, subject, body, metadata=None):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return True

    def to(self) -> list[str]:
        return sorted(m["to"] for m in self.sent)


@pytest.fixture
def outbox():
    box = Outbox()
    with patch("app.notifications.get_channel", return_value=box):
        yield box


def _course(db, tenant_id, name="SOC Analyst"):
    c = Course(id=uuid.uuid4(), name=name, tenant_id=tenant_id, is_published=True)
    db.add(c)
    db.flush()
    return c


def _student_row(db, tenant_id, email, *, source="ad", role=UserRole.student, active=True) -> User:
    u = User(
        id=uuid.uuid4(), keycloak_id=f"{'csv-import:' if source == 'csv_import' else 'kc-'}{uuid.uuid4()}",
        email=email, display_name=email.split("@")[0], role=role, tenant_id=tenant_id, source=source,
        is_active=active,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def world(db_session):
    db = db_session
    a, b = real_tenant(db, "cls-a"), real_tenant(db, "cls-b")
    w = {
        "a": a, "b": b,
        "lead": real_user(db, UserRole.instructor, a.id),
        "assistant": real_user(db, UserRole.instructor, a.id),
        "other_instructor": real_user(db, UserRole.instructor, a.id),
        "observer": real_user(db, UserRole.observer, a.id),
        "admin": real_user(db, UserRole.admin, a.id),
        "range_ops": real_user(db, UserRole.range_ops, a.id),
        "student": real_user(db, UserRole.student, a.id),
        "b_admin": real_user(db, UserRole.admin, b.id),
        "b_instructor": real_user(db, UserRole.instructor, b.id),
        "course": _course(db, a.id),
        "shared": _course(db, None, "Shared catalogue course"),
        "b_course": _course(db, b.id, "B course"),
    }
    return w


def _body(w, **over):
    body = {
        "name": "C201-26-03",
        "course_id": str(w["course"].id),
        "start_date": str(TODAY + timedelta(days=7)),
        "end_date": str(TODAY + timedelta(days=11)),
        "assistant_ids": [w["assistant"].id],
        "observer_ids": [w["observer"].id],
    }
    body.update(over)
    return body


@pytest.fixture
def klass(client, world):
    """A class in tenant A led by ``lead``, with an assistant and an observer."""
    act_as(world["lead"])
    r = client.post("/classes", json=_body(world))
    assert r.status_code == 201, r.text
    return r.json()


# ── Creating a class ──────────────────────────────────────────────────────


def test_an_instructor_creates_a_class_and_leads_it(client, world, klass):
    assert klass["lead_instructor"]["id"] == world["lead"].id
    assert klass["my_role"] == "lead" and klass["can_edit"] is True
    assert [p["id"] for p in klass["assistants"]] == [world["assistant"].id]
    assert [p["id"] for p in klass["observers"]] == [world["observer"].id]
    assert klass["tenant_id"] == str(world["a"].id)
    assert klass["course_name"] == "SOC Analyst"


def test_an_admin_creates_a_class_for_an_instructor(client, world):
    act_as(world["admin"])
    r = client.post("/classes", json=_body(world, lead_instructor_id=world["other_instructor"].id))
    assert r.status_code == 201, r.text
    assert r.json()["lead_instructor"]["id"] == world["other_instructor"].id
    assert r.json()["my_role"] == "admin"


@pytest.mark.parametrize("who", ["student", "observer", "range_ops"])
def test_only_instructors_and_admins_create_classes(client, world, who):
    act_as(world[who])
    assert client.post("/classes", json=_body(world)).status_code == 403


def test_a_class_is_always_in_the_callers_own_tenant(client, world):
    """Another tenant's course or instructor is refused as unknown; the tenant is never chosen."""
    act_as(world["lead"])
    assert client.post("/classes", json=_body(world, course_id=str(world["b_course"].id))).status_code == 422
    r = client.post("/classes", json=_body(world, lead_instructor_id=world["b_instructor"].id))
    assert r.status_code == 422
    assert "another" not in r.text.lower()
    r = client.post("/classes", json=_body(world, course_id=str(world["shared"].id), assistant_ids=[], observer_ids=[]))
    assert r.status_code == 201 and r.json()["tenant_id"] == str(world["a"].id)


def test_staff_must_hold_staff_roles(client, world):
    act_as(world["lead"])
    assert client.post("/classes", json=_body(world, assistant_ids=[world["student"].id])).status_code == 422
    assert client.post("/classes", json=_body(world, observer_ids=[world["student"].id])).status_code == 422
    assert client.post("/classes", json=_body(world, lead_instructor_id=world["observer"].id)).status_code == 422


def test_end_before_start_is_refused(client, world):
    act_as(world["lead"])
    r = client.post("/classes", json=_body(world, end_date=str(TODAY)))
    assert r.status_code == 422


# ── Who sees and changes a class ──────────────────────────────────────────


@pytest.mark.parametrize("who,role", [("lead", "lead"), ("assistant", "assistant"), ("observer", "observer"),
                                      ("admin", "admin")])
def test_staff_and_admins_see_the_class_and_its_roll_call(client, world, klass, who, role):
    act_as(world[who])
    r = client.get(f"/classes/{klass['id']}")
    assert r.status_code == 200 and r.json()["my_role"] == role
    assert client.get(f"/classes/{klass['id']}/roll-call").status_code == 200
    assert klass["id"] in [c["id"] for c in client.get("/classes").json()]


@pytest.mark.parametrize("who", ["other_instructor", "student", "range_ops"])
def test_anyone_else_in_the_tenant_does_not_find_it(client, world, klass, who):
    act_as(world[who])
    assert client.get(f"/classes/{klass['id']}").status_code == 404
    assert client.get(f"/classes/{klass['id']}/roll-call").status_code == 404
    assert client.post(f"/classes/{klass['id']}/roll-call", json={"text": "x@corp.test"}).status_code == 404
    assert klass["id"] not in [c["id"] for c in client.get("/classes").json()]


@pytest.mark.parametrize("who", ["b_admin", "b_instructor"])
def test_another_tenant_never_finds_it(client, world, klass, who):
    act_as(world[who])
    cid = klass["id"]
    assert client.get(f"/classes/{cid}").status_code == 404
    assert client.get(f"/classes/{cid}/roll-call").status_code == 404
    assert client.put(f"/classes/{cid}", json=_body(world)).status_code == 404
    assert client.delete(f"/classes/{cid}").status_code == 404
    assert client.post(f"/classes/{cid}/roll-call", json={"text": "x@corp.test"}).status_code == 404
    assert client.get("/classes").json() == []


@pytest.mark.parametrize("who", ["assistant", "observer"])
def test_assistants_and_observers_read_but_do_not_change(client, world, klass, who, outbox):
    act_as(world[who])
    cid = klass["id"]
    assert client.post(f"/classes/{cid}/roll-call", json={"text": "x@corp.test"}).status_code == 403
    assert client.post(f"/classes/{cid}/roll-call/csv", files={"file": ("r.csv", b"email\nx@corp.test\n")}).status_code == 403
    assert client.put(f"/classes/{cid}", json=_body(world)).status_code == 403
    assert client.delete(f"/classes/{cid}").status_code == 403
    assert outbox.sent == []


def test_a_student_on_the_roll_call_sees_the_class_but_not_the_roll_call(client, world, klass, db_session, outbox):
    act_as(world["lead"])
    client.post(f"/classes/{klass['id']}/roll-call", json={"emails": [world["student"].email]})
    act_as(world["student"])
    r = client.get(f"/classes/{klass['id']}")
    assert r.status_code == 200 and r.json()["my_role"] == "student" and r.json()["can_edit"] is False
    assert client.get(f"/classes/{klass['id']}/roll-call").status_code == 403
    assert client.post(f"/classes/{klass['id']}/roll-call", json={"text": "x@corp.test"}).status_code == 403


def test_my_classes_lists_the_ones_i_staff(client, world, klass):
    for who in ("lead", "assistant", "observer"):
        act_as(world[who])
        assert [c["id"] for c in client.get("/classes/mine").json()] == [klass["id"]]
    act_as(world["other_instructor"])
    assert client.get("/classes/mine").json() == []
    act_as(world["student"])
    assert client.get("/classes/mine").status_code == 403


def test_the_lead_hands_the_class_over(client, world, klass, db_session):
    act_as(world["lead"])
    body = _body(world, lead_instructor_id=world["other_instructor"].id)
    r = client.put(f"/classes/{klass['id']}", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["lead_instructor"]["id"] == world["other_instructor"].id
    assert r.json()["my_role"] is None
    assert client.get(f"/classes/{klass['id']}").status_code == 404
    act_as(world["other_instructor"])
    assert client.get(f"/classes/{klass['id']}").json()["can_edit"] is True
    assert db_session.query(AuditLog).filter_by(resource_id=klass["id"], action="class_update").count() == 1


def test_cancel_keeps_the_record_and_hides_it(client, world, klass, db_session):
    act_as(world["lead"])
    assert client.delete(f"/classes/{klass['id']}").status_code == 204
    assert db_session.get(Class, uuid.UUID(klass["id"])).status == "cancelled"
    assert client.get("/classes").json() == []
    assert len(client.get("/classes?include_cancelled=true").json()) == 1
    assert client.post(f"/classes/{klass['id']}/roll-call", json={"text": "x@corp.test"}).status_code == 409


# ── The roll call ─────────────────────────────────────────────────────────


def test_paste_adds_new_students_and_emails_them_to_onboard(client, world, klass, outbox, db_session):
    act_as(world["lead"])
    text = "Joe Bloggs <Joe.Bloggs@Corp.Test>, jane@corp.test; not-an-email@\njane@corp.test"
    r = client.post(f"/classes/{klass['id']}/roll-call", json={"text": text})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(e["email"] for e in body["added"]) == ["jane@corp.test", "joe.bloggs@corp.test"]
    assert all(e["status"] == "invited" for e in body["added"])
    assert body["invalid"] == ["not-an-email@"]
    assert outbox.to() == ["jane@corp.test", "joe.bloggs@corp.test"]
    assert "C201-26-03" in outbox.sent[0]["subject"]
    assert "no access request" in outbox.sent[0]["body"]
    again = client.post(f"/classes/{klass['id']}/roll-call", json={"emails": ["JANE@corp.test"]}).json()
    assert again["already_on_roll_call"] == ["jane@corp.test"] and again["added"] == []
    assert len(outbox.sent) == 2  # not emailed twice


def test_csv_upload_reads_the_email_column(client, world, klass, outbox):
    act_as(world["lead"])
    csv = b"name,email\nJoe,joe@corp.test\nAnn,ann@corp.test\nBad,nope\n"
    r = client.post(f"/classes/{klass['id']}/roll-call/csv", files={"file": ("roster.csv", csv, "text/csv")})
    assert r.status_code == 200, r.text
    assert sorted(e["email"] for e in r.json()["added"]) == ["ann@corp.test", "joe@corp.test"]
    headerless = b"kim@corp.test\nlee@corp.test\n"
    r = client.post(f"/classes/{klass['id']}/roll-call/csv", files={"file": ("r.csv", headerless, "text/csv")})
    assert sorted(e["email"] for e in r.json()["added"]) == ["kim@corp.test", "lee@corp.test"]


def test_an_existing_student_is_linked_and_enrolled_at_once(client, world, klass, outbox, db_session):
    """#272: a cohort is enrolled on the class's course in one action, with no approval queue."""
    act_as(world["lead"])
    r = client.post(f"/classes/{klass['id']}/roll-call", json={"emails": [world["student"].email.upper()]})
    entry = r.json()["added"][0]
    assert entry["status"] == "signed_in" and entry["user_id"] == world["student"].id
    assert outbox.sent == []  # they have an account already
    enrolled = db_session.query(Enrollment).filter_by(user_id=uuid.UUID(world["student"].id),
                                                       course_id=world["course"].id).one()
    assert enrolled.tenant_id == world["a"].id


def test_staff_and_disabled_accounts_are_refused(client, world, klass, db_session, outbox):
    disabled = _student_row(db_session, world["a"].id, "gone@corp.test", active=False)
    act_as(world["lead"])
    r = client.post(f"/classes/{klass['id']}/roll-call",
                    json={"emails": [world["other_instructor"].email, disabled.email]}).json()
    assert r["added"] == []
    reasons = {x["email"]: x["reason"] for x in r["refused"]}
    assert "instructor" in reasons[world["other_instructor"].email]
    assert "deactivated" in reasons["gone@corp.test"]


def test_an_address_another_tenant_uses_is_not_disclosed(client, world, klass, db_session, outbox):
    b_student = _student_row(db_session, world["b"].id, "b.student@corp.test")
    act_as(world["lead"])
    r = client.post(f"/classes/{klass['id']}/roll-call", json={"emails": [b_student.email]}).json()
    assert r["refused"] == [] and r["added"][0]["status"] == "invited" and r["added"][0]["user_id"] is None
    assert db_session.query(Enrollment).filter_by(user_id=b_student.id).count() == 0


def test_removing_marks_the_entry_and_keeps_it(client, world, klass, db_session, outbox):
    act_as(world["lead"])
    added = client.post(f"/classes/{klass['id']}/roll-call", json={"text": "joe@corp.test"}).json()["added"][0]
    assert client.delete(f"/classes/{klass['id']}/roll-call/{added['id']}").status_code == 204
    rows = client.get(f"/classes/{klass['id']}/roll-call").json()
    assert rows[0]["status"] == "removed" and rows[0]["removed_at"]
    assert client.get(f"/classes/{klass['id']}").json()["roll_call_count"] == 0
    assert db_session.query(AuditLog).filter_by(resource_id=klass["id"], action="roll_call_remove").count() == 1
    act_as(world["assistant"])
    assert client.delete(f"/classes/{klass['id']}/roll-call/{added['id']}").status_code == 403


def test_an_entry_of_another_class_is_not_found(client, world, klass, outbox):
    act_as(world["lead"])
    other = client.post("/classes", json=_body(world, name="C201-26-04", assistant_ids=[], observer_ids=[])).json()
    e = client.post(f"/classes/{other['id']}/roll-call", json={"text": "joe@corp.test"}).json()["added"][0]
    assert client.delete(f"/classes/{klass['id']}/roll-call/{e['id']}").status_code == 404


# ── Roll call is approval ─────────────────────────────────────────────────


def _identity(email="joe.bloggs@corp.test", sub="ad-sub-joe", **over) -> TokenPayload:
    data = {
        "sub": sub, "email": email, "email_verified": True, "given_name": "Joe", "family_name": "Bloggs",
        "preferred_username": "joe", "groups": [], "ad_object_guid": str(uuid.uuid5(uuid.NAMESPACE_OID, sub)),
        "ad_distinguished_name": f"CN={sub},OU=Users,DC=corp,DC=test",
    }
    data.update(over)
    return TokenPayload(**data)


@pytest.fixture
def sign_in(client):
    def _go(identity: TokenPayload) -> dict:
        fastapi_app.dependency_overrides[get_token_identity] = lambda: identity
        r = client.get("/auth/me")
        assert r.status_code == 200, r.text
        return r.json()

    yield _go
    fastapi_app.dependency_overrides.pop(get_token_identity, None)


def _on_roll_call(client, world, klass, *emails):
    act_as(world["lead"])
    r = client.post(f"/classes/{klass['id']}/roll-call", json={"emails": list(emails)})
    assert r.status_code == 200, r.text
    return r.json()["added"]


def test_a_roll_call_student_goes_straight_in(client, world, klass, sign_in, db_session, outbox):
    entry = _on_roll_call(client, world, klass, "joe.bloggs@corp.test")[0]
    body = sign_in(_identity(email="Joe.Bloggs@CORP.test"))  # case-insensitive
    assert body["status"] == "registered"
    user = db_session.query(User).filter_by(keycloak_id="ad-sub-joe").one()
    assert user.role == UserRole.student and user.tenant_id == world["a"].id and user.source == "ad"
    assert db_session.query(Enrollment).filter_by(user_id=user.id, course_id=world["course"].id).count() == 1
    row = db_session.get(RollCallEntry, uuid.UUID(entry["id"]))
    assert row.status == "signed_in" and row.user_id == user.id and row.signed_in_at is not None
    audit = db_session.query(AuditLog).filter_by(action="roll_call_admit", resource_id=klass["id"]).one()
    assert audit.user_id == user.id and audit.tenant_id == world["a"].id
    req = db_session.query(RegistrationRequest).filter_by(keycloak_id="ad-sub-joe").one()
    assert req.status == RegistrationStatus.approved and str(req.decided_by_user_id) == world["lead"].id
    # Signing in again changes nothing.
    assert sign_in(_identity())["status"] == "registered"
    assert db_session.query(AuditLog).filter_by(action="roll_call_admit").count() == 1


def test_no_match_is_request_access(client, world, klass, sign_in, db_session, outbox):
    _on_roll_call(client, world, klass, "someone.else@corp.test")
    assert sign_in(_identity())["status"] == "unregistered"
    assert db_session.query(User).filter_by(keycloak_id="ad-sub-joe").count() == 0


def test_a_near_miss_is_not_a_match(client, world, klass, sign_in, db_session, outbox):
    _on_roll_call(client, world, klass, "joe.bloggs@corp.test")
    for email in ("joe.bloggs@corp.test.evil", "xjoe.bloggs@corp.test", "joe.bloggs@corp.tes"):
        assert sign_in(_identity(email=email))["status"] == "unregistered"
    assert db_session.query(User).filter_by(keycloak_id="ad-sub-joe").count() == 0


def test_the_same_email_on_two_tenants_roll_calls_is_not_guessed(client, world, klass, sign_in, db_session, outbox):
    _on_roll_call(client, world, klass, "joe.bloggs@corp.test")
    act_as(world["b_instructor"])
    b = client.post("/classes", json=_body(world, course_id=str(world["b_course"].id), assistant_ids=[],
                                           observer_ids=[])).json()
    client.post(f"/classes/{b['id']}/roll-call", json={"emails": ["joe.bloggs@corp.test"]})
    assert sign_in(_identity())["status"] == "unregistered"
    assert db_session.query(User).filter_by(keycloak_id="ad-sub-joe").count() == 0
    assert db_session.query(RollCallEntry).filter_by(status="signed_in").count() == 0


def test_two_classes_in_one_tenant_admit_once_and_enrol_on_both(client, world, klass, sign_in, db_session, outbox):
    other_course = _course(db_session, world["a"].id, "Network Defence")
    _on_roll_call(client, world, klass, "joe.bloggs@corp.test")
    act_as(world["lead"])
    k2 = client.post("/classes", json=_body(world, name="ND-1", course_id=str(other_course.id), assistant_ids=[],
                                            observer_ids=[])).json()
    client.post(f"/classes/{k2['id']}/roll-call", json={"emails": ["joe.bloggs@corp.test"]})
    assert sign_in(_identity())["status"] == "registered"
    user = db_session.query(User).filter_by(keycloak_id="ad-sub-joe").one()
    courses = {e.course_id for e in db_session.query(Enrollment).filter_by(user_id=user.id)}
    assert courses == {world["course"].id, other_course.id}


def test_a_staff_email_is_never_adopted(client, world, klass, sign_in, db_session, outbox):
    """A staff row created (or renamed) after the entry was added: the roll call must not hand
    it to whoever signs in with that address."""
    _on_roll_call(client, world, klass, "joe.bloggs@corp.test")
    staff = _student_row(db_session, world["a"].id, "joe.bloggs@corp.test", source="csv_import",
                         role=UserRole.instructor)
    before = staff.keycloak_id
    assert sign_in(_identity())["status"] == "unregistered"
    db_session.refresh(staff)
    assert staff.keycloak_id == before and staff.role == UserRole.instructor
    assert db_session.query(RollCallEntry).filter_by(status="signed_in").count() == 0
    assert db_session.query(RegistrationRequest).filter_by(keycloak_id="ad-sub-joe").count() == 0


def test_a_roster_student_is_adopted(client, world, klass, sign_in, db_session, outbox):
    """A pre-provisioned (CSV) Student row is the account the AD identity takes over."""
    roster = _student_row(db_session, world["a"].id, "Joe.Bloggs@corp.test", source="csv_import")
    entry = _on_roll_call(client, world, klass, "joe.bloggs@corp.test")[0]
    assert entry["status"] == "invited" and entry["user_id"] == str(roster.id)
    assert outbox.to() == ["joe.bloggs@corp.test"]
    assert sign_in(_identity())["status"] == "registered"
    db_session.refresh(roster)
    assert roster.keycloak_id == "ad-sub-joe" and roster.source == "ad"
    assert db_session.query(User).filter(User.email.ilike("joe.bloggs@corp.test")).count() == 1


def test_a_removed_entry_does_not_admit(client, world, klass, sign_in, db_session, outbox):
    entry = _on_roll_call(client, world, klass, "joe.bloggs@corp.test")[0]
    client.delete(f"/classes/{klass['id']}/roll-call/{entry['id']}")
    assert sign_in(_identity())["status"] == "unregistered"


def test_a_cancelled_or_finished_class_does_not_admit(client, world, klass, sign_in, db_session, outbox):
    _on_roll_call(client, world, klass, "joe.bloggs@corp.test")
    cls = db_session.get(Class, uuid.UUID(klass["id"]))
    cls.start_date, cls.end_date = TODAY - timedelta(days=5), TODAY - timedelta(days=1)
    db_session.flush()
    assert sign_in(_identity())["status"] == "unregistered"
    cls.end_date, cls.status = TODAY + timedelta(days=3), "cancelled"
    db_session.flush()
    assert sign_in(_identity())["status"] == "unregistered"


@pytest.mark.parametrize("over", [
    {"email_verified": False},
    {"ad_object_guid": None},
    {"ad_object_guid": "  "},
    {"tn_session": "lti"},
])
def test_only_a_verified_ad_email_counts(client, world, klass, sign_in, db_session, outbox, over):
    _on_roll_call(client, world, klass, "joe.bloggs@corp.test")
    assert sign_in(_identity(**over))["status"] == "unregistered"
    assert db_session.query(User).filter_by(keycloak_id="ad-sub-joe").count() == 0


def test_a_lead_who_left_the_tenant_admits_nobody(client, world, klass, sign_in, db_session, outbox):
    _on_roll_call(client, world, klass, "joe.bloggs@corp.test")
    lead = db_session.get(User, uuid.UUID(world["lead"].id))
    lead.is_active = False
    db_session.flush()
    assert sign_in(_identity())["status"] == "unregistered"


def test_the_idp_email_verified_claim_is_read(world):
    from app.auth import token_from_idp_claims

    assert token_from_idp_claims({"sub": "s", "email": "a@b.c", "email_verified": True}).email_verified is True
    assert token_from_idp_claims({"sub": "s", "email": "a@b.c"}).email_verified is False


# ── The day-before reminder ───────────────────────────────────────────────


def test_one_reminder_the_day_before_to_those_not_signed_in(client, world, klass, db_session, outbox):
    _on_roll_call(client, world, klass, "joe@corp.test", world["student"].email)
    start = date.fromisoformat(klass["start_date"])
    eve = datetime.combine(start - timedelta(days=1), datetime.min.time(), tzinfo=UTC).replace(hour=9)
    assert clock.tick(db_session, now=eve - timedelta(days=2)) == []  # not yet
    mails = clock.tick(db_session, now=eve)
    assert [m.to for m in mails] == ["joe@corp.test"]  # not the Student who has an account
    assert "tomorrow" in mails[0].subject
    assert clock.tick(db_session, now=eve + timedelta(hours=1)) == []  # once
    assert db_session.query(AuditLog).filter_by(action="roll_call_reminder", resource_id=klass["id"]).count() == 1


def test_no_reminder_for_a_removed_entry_or_a_cancelled_class(client, world, klass, db_session, outbox):
    e = _on_roll_call(client, world, klass, "joe@corp.test", "ann@corp.test")
    client.delete(f"/classes/{klass['id']}/roll-call/{e[0]['id']}")
    start = date.fromisoformat(klass["start_date"])
    eve = datetime.combine(start - timedelta(days=1), datetime.min.time(), tzinfo=UTC).replace(hour=9)
    cls = db_session.get(Class, uuid.UUID(klass["id"]))
    cls.status = "cancelled"
    db_session.flush()
    assert clock.tick(db_session, now=eve) == []
    cls.status = "open"
    db_session.flush()
    remaining = {x["email"] for x in e[1:]}
    assert {m.to for m in clock.tick(db_session, now=eve)} == remaining


# ── The invite list is the roll call, tenant-pinned (#291 review) ─────────


def test_roll_call_emails_drop_a_student_who_moved_tenant(client, world, klass, db_session, outbox):
    _on_roll_call(client, world, klass, world["student"].email, "new@corp.test")
    cls = db_session.get(Class, uuid.UUID(klass["id"]))
    assert service.class_roll_call_emails(db_session, cls) == sorted([world["student"].email, "new@corp.test"])
    moved = db_session.get(User, uuid.UUID(world["student"].id))
    moved.tenant_id = world["b"].id
    db_session.flush()
    assert service.class_roll_call_emails(db_session, cls) == ["new@corp.test"]


# ── The migration ─────────────────────────────────────────────────────────

API = Path(__file__).resolve().parents[2] / "control-plane" / "api"


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True,
                          text=True, timeout=300)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]


def test_the_migration_upgrades_and_downgrades(tmp_path):
    import sqlalchemy as sa

    url = f"sqlite:///{tmp_path / 'classes.db'}"
    tables = {"classes", "class_staff", "class_roll_call"}
    _alembic(url, "upgrade", "head")
    eng = sa.create_engine(url)
    try:
        insp = sa.inspect(eng)
        assert tables <= set(insp.get_table_names())
        assert {c["name"] for c in insp.get_columns("class_roll_call")} >= {"email", "status", "user_id", "tenant_id"}
    finally:
        eng.dispose()
    _alembic(url, "downgrade", "6a5f8e732b95-1")
    eng = sa.create_engine(url)
    try:
        assert not tables & set(sa.inspect(eng).get_table_names())
    finally:
        eng.dispose()
    _alembic(url, "upgrade", "head")
