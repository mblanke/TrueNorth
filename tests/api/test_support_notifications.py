"""In-app notifications for Support: who hears about what, and that each user sees only their own."""

import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import User, UserRole

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


def _person(db, role: UserRole, name: str, tenant: str = DEV_TENANT) -> CurrentUser:
    uid = uuid.uuid4()
    db.add(
        User(
            id=uid,
            keycloak_id=f"kc-{uid.hex}",
            email=f"{uid.hex[:8]}@example.test",
            display_name=name,
            role=role,
            tenant_id=uuid.UUID(tenant),
        )
    )
    db.commit()
    return CurrentUser(
        id=str(uid), email="x", display_name=name, role=role, tenant_id=tenant, keycloak_id=f"kc-{uid.hex}"
    )


@contextmanager
def acting_as(who: CurrentUser):
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def people(db_session):
    return {
        "student": _person(db_session, UserRole.student, "Cpl Lee"),
        "chen": _person(db_session, UserRole.range_ops, "Sgt Chen"),
        "shaw": _person(db_session, UserRole.instructor, "Capt Shaw"),
    }


def _inbox(client, who):
    with acting_as(who):
        return [(n["title"], n["kind"]) for n in client.get("/notifications").json()]


def _file(client, who, **extra):
    with acting_as(who):
        r = client.post("/tickets", json={"subject": "dc01 won't boot", **extra})
        assert r.status_code == 201
        return r.json()


class TestWhoIsTold:
    def test_a_new_ticket_tells_staff_not_the_reporter(self, client, people):
        t = _file(client, people["student"])
        assert _inbox(client, people["chen"]) == [(f"{t['key']}: new ticket", "ticket_new")]
        assert _inbox(client, people["shaw"]) == [(f"{t['key']}: new ticket", "ticket_new")]
        assert _inbox(client, people["student"]) == []

    def test_assignment_tells_the_assignee(self, client, people):
        t = _file(client, people["student"])
        with acting_as(people["shaw"]):
            client.patch(f"/tickets/{t['id']}", json={"assignee_id": people["chen"].id})
        assert (f"{t['key']}: assigned to you", "ticket_assigned") in _inbox(client, people["chen"])

    def test_assigning_yourself_tells_nobody(self, client, people):
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            client.post("/notifications/read-all")
            client.patch(f"/tickets/{t['id']}", json={"assignee_id": people["chen"].id})
            assert client.get("/notifications/unread-count").json() == {"unread": 0}

    def test_staff_reply_and_status_tell_the_reporter(self, client, people):
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            client.post(f"/tickets/{t['id']}/comments", json={"body": "Rebooting the host now.\nMore detail."})
            client.patch(f"/tickets/{t['id']}", json={"status": "resolved"})
        with acting_as(people["student"]):
            inbox = client.get("/notifications").json()
        kinds = {n["kind"]: n for n in inbox}
        assert kinds["ticket_reply"]["message"] == "Rebooting the host now."
        assert kinds["ticket_status"]["title"] == f"{t['key']}: now resolved"
        assert kinds["ticket_reply"]["link"] == f"/support/{t['id']}"

    def test_internal_notes_never_reach_the_reporter(self, client, people):
        t = _file(client, people["student"])
        with acting_as(people["shaw"]):
            client.patch(f"/tickets/{t['id']}", json={"assignee_id": people["chen"].id})
            client.post(f"/tickets/{t['id']}/comments", json={"body": "user error", "is_internal": True})
        assert _inbox(client, people["student"]) == []
        assert (f"{t['key']}: internal note", "ticket_note") in _inbox(client, people["chen"])

    def test_reporter_reply_goes_to_the_assignee_or_all_staff(self, client, people):
        t = _file(client, people["student"])
        with acting_as(people["student"]):
            client.post(f"/tickets/{t['id']}/comments", json={"body": "any news?"})
        assert (f"{t['key']}: reporter replied", "ticket_reply") in _inbox(client, people["shaw"])
        with acting_as(people["shaw"]):
            client.patch(f"/tickets/{t['id']}", json={"assignee_id": people["chen"].id})
            client.post("/notifications/read-all")
        with acting_as(people["student"]):
            client.post(f"/tickets/{t['id']}/comments", json={"body": "still broken"})
        with acting_as(people["shaw"]):
            assert client.get("/notifications/unread-count").json() == {"unread": 0}
        assert _inbox(client, people["chen"])[0] == (f"{t['key']}: reporter replied", "ticket_reply")

    def test_moving_a_card_tells_the_reporter(self, client, people):
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            client.post(f"/tickets/{t['id']}/move", json={"status": "in_progress", "board_order": 1})
        assert (f"{t['key']}: now in progress", "ticket_status") in _inbox(client, people["student"])


class TestOwnOnly:
    def test_read_count_and_read_all(self, client, people):
        _file(client, people["student"])
        _file(client, people["student"])
        with acting_as(people["chen"]):
            assert client.get("/notifications/unread-count").json() == {"unread": 2}
            first = client.get("/notifications").json()[0]
            r = client.post(f"/notifications/{first['id']}/read")
            assert r.status_code == 200 and r.json()["read"] is True
            assert client.get("/notifications/unread-count").json() == {"unread": 1}
            assert len(client.get("/notifications?unread_only=true").json()) == 1
            client.post("/notifications/read-all")
            assert client.get("/notifications/unread-count").json() == {"unread": 0}

    def test_someone_elses_notification_is_404(self, client, people, db_session):
        _file(client, people["student"])
        with acting_as(people["chen"]):
            nid = client.get("/notifications").json()[0]["id"]
        with acting_as(people["shaw"]):
            assert client.post(f"/notifications/{nid}/read").status_code == 404
        outsider = _person(db_session, UserRole.admin, "Other admin", tenant=OTHER_TENANT)
        with acting_as(outsider):
            assert client.get("/notifications").json() == []
            assert client.post(f"/notifications/{nid}/read").status_code == 404
