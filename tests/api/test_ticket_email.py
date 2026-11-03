"""Ticket notifications by email (#378): who is mailed, what the mail says, and the opt-out."""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app import notifications as notifications_pkg
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import User, UserRole
from app.notifications import ticket_email
from app.notifications.models import UserNotification

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
SECRET = "the dc01 admin password is hunter2"


def _person(db, role: UserRole, name: str, email: str | None = None) -> CurrentUser:
    uid = uuid.uuid4()
    db.add(
        User(
            id=uid,
            keycloak_id=f"kc-{uid.hex}",
            email=email if email is not None else f"{uid.hex[:8]}@example.test",
            display_name=name,
            role=role,
            tenant_id=uuid.UUID(DEV_TENANT),
        )
    )
    db.commit()
    return CurrentUser(
        id=str(uid), email="x", display_name=name, role=role, tenant_id=DEV_TENANT, keycloak_id=f"kc-{uid.hex}"
    )


@contextmanager
def acting_as(who: CurrentUser):
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


class FakeChannel:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.fail = False

    async def send(self, to, subject, body):
        if self.fail:
            raise RuntimeError("relay down")
        self.sent.append((to, subject, body))
        return True


@pytest.fixture
def mail(monkeypatch):
    """SMTP on, WEB_BASE_URL set, and the channel replaced by one that records."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_FROM", "truenorth@example.test")
    monkeypatch.setenv("WEB_BASE_URL", "https://tn.example.test/")
    chan = FakeChannel()
    monkeypatch.setattr(notifications_pkg, "get_channel", lambda name: chan)
    return chan


@pytest.fixture
def people(db_session):
    return {
        "student": _person(db_session, UserRole.student, "Cpl Lee"),
        "chen": _person(db_session, UserRole.range_ops, "Sgt Chen"),
        "shaw": _person(db_session, UserRole.instructor, "Capt Shaw"),
    }


def _file(client, who, **extra):
    with acting_as(who):
        r = client.post("/tickets", json={"subject": "dc01 won't boot", "details": SECRET, **extra})
        assert r.status_code == 201
        return r.json()


def _age_notifications(db_session, minutes: int = 10) -> None:
    for n in db_session.query(UserNotification).all():
        n.created_at = datetime.now(UTC) - timedelta(minutes=minutes)
    db_session.commit()


def _email_of(db_session, who: CurrentUser) -> str:
    return db_session.get(User, uuid.UUID(who.id)).email


class TestWhoIsMailed:
    def test_a_new_ticket_is_bell_only(self, client, people, mail):
        _file(client, people["student"])
        assert mail.sent == []

    def test_assignment_mails_the_assignee_without_any_ticket_text(self, client, db_session, people, mail):
        t = _file(client, people["student"], subject=SECRET)
        with acting_as(people["shaw"]):
            client.patch(f"/tickets/{t['id']}", json={"assignee_id": people["chen"].id})
        assert len(mail.sent) == 1
        to, subject, body = mail.sent[0]
        assert to == _email_of(db_session, people["chen"])
        assert "assigned to you" in subject and t["key"] in subject
        assert f"https://tn.example.test/support/{t['id']}" in body
        for text in (SECRET, "hunter2", "dc01", "Cpl Lee", "Capt Shaw"):
            assert text not in subject and text not in body

    def test_a_staff_reply_mails_the_reporter(self, client, db_session, people, mail):
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            client.post(f"/tickets/{t['id']}/comments", json={"body": "Rebooting. " + SECRET})
        assert [m[0] for m in mail.sent] == [_email_of(db_session, people["student"])]
        assert SECRET not in mail.sent[0][2]

    def test_the_actor_is_never_mailed(self, client, people, mail):
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            client.patch(f"/tickets/{t['id']}", json={"assignee_id": people["chen"].id})
        assert mail.sent == []

    def test_a_reporter_reply_to_an_unowned_ticket_is_bell_only(self, client, people, mail):
        t = _file(client, people["student"])
        with acting_as(people["student"]):
            client.post(f"/tickets/{t['id']}/comments", json={"body": "any news?"})
        assert mail.sent == []

    def test_a_reporter_reply_to_an_owned_ticket_mails_the_owner(self, client, db_session, people, mail):
        t = _file(client, people["student"])
        with acting_as(people["shaw"]):
            client.patch(f"/tickets/{t['id']}", json={"assignee_id": people["chen"].id})
        mail.sent.clear()
        _age_notifications(db_session)  # the assignment was a moment ago: that would throttle this one
        with acting_as(people["student"]):
            client.post(f"/tickets/{t['id']}/comments", json={"body": "any news?"})
        assert [m[0] for m in mail.sent] == [_email_of(db_session, people["chen"])]

    def test_an_internal_note_never_mails_the_reporter(self, client, people, mail):
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            client.post(f"/tickets/{t['id']}/comments", json={"body": "private", "is_internal": True})
        assert all("student" not in m[0] for m in mail.sent)
        assert not any(m[0] == people["student"].email for m in mail.sent)

    def test_a_status_change_mails_the_reporter(self, client, db_session, people, mail):
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            client.patch(f"/tickets/{t['id']}", json={"status": "resolved"})
        assert [m[0] for m in mail.sent] == [_email_of(db_session, people["student"])]
        assert "status" in mail.sent[0][1].lower() or t["key"] in mail.sent[0][1]


class TestWhenNotToMail:
    def test_nothing_is_sent_without_smtp(self, client, people, mail, monkeypatch):
        monkeypatch.delenv("SMTP_HOST")
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            client.patch(f"/tickets/{t['id']}", json={"status": "resolved"})
        assert mail.sent == []

    def test_a_person_with_no_address_or_who_is_disabled_is_skipped(self, client, db_session, people, mail):
        no_email = _person(db_session, UserRole.student, "No Mail", email="")
        gone = _person(db_session, UserRole.student, "Gone")
        db_session.get(User, uuid.UUID(gone.id)).is_active = False
        db_session.commit()
        for who in (no_email, gone):
            t = _file(client, who)
            with acting_as(people["chen"]):
                client.patch(f"/tickets/{t['id']}", json={"status": "resolved"})
        assert mail.sent == []

    def test_a_busy_thread_sends_one_email_not_ten(self, client, people, mail):
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            for i in range(5):
                client.post(f"/tickets/{t['id']}/comments", json={"body": f"update {i}"})
        assert len(mail.sent) == 1

    def test_a_failed_send_never_fails_the_request_or_the_bell(self, client, people, mail):
        mail.fail = True
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            r = client.patch(f"/tickets/{t['id']}", json={"status": "resolved"})
            assert r.status_code == 200
        with acting_as(people["student"]):
            assert client.get("/notifications/unread-count").json()["unread"] >= 1

    def test_a_rolled_back_request_sends_nothing(self, client, people, mail):
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            r = client.patch(f"/tickets/{t['id']}", json={"status": "not-a-status"})
            assert r.status_code == 422
        assert mail.sent == []


class TestOptOut:
    def test_defaults_to_on_and_reports_whether_mail_is_available(self, client, people, mail, monkeypatch):
        with acting_as(people["student"]):
            assert client.get("/notifications/settings").json() == {"email_tickets": True, "email_available": True}
            monkeypatch.delenv("SMTP_HOST")
            assert client.get("/notifications/settings").json()["email_available"] is False

    def test_turning_it_off_stops_the_mail_but_not_the_bell(self, client, people, mail):
        with acting_as(people["student"]):
            r = client.put("/notifications/settings", json={"email_tickets": False})
            assert r.status_code == 200 and r.json()["email_tickets"] is False
            assert client.get("/notifications/settings").json()["email_tickets"] is False
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            client.patch(f"/tickets/{t['id']}", json={"status": "resolved"})
        assert mail.sent == []
        with acting_as(people["student"]):
            assert client.get("/notifications/unread-count").json()["unread"] == 1

    def test_turning_it_back_on_mails_again(self, client, people, mail):
        with acting_as(people["student"]):
            client.put("/notifications/settings", json={"email_tickets": False})
            client.put("/notifications/settings", json={"email_tickets": True})
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            client.patch(f"/tickets/{t['id']}", json={"status": "resolved"})
        assert len(mail.sent) == 1

    def test_the_switch_is_per_person(self, client, people, mail):
        with acting_as(people["student"]):
            client.put("/notifications/settings", json={"email_tickets": False})
        with acting_as(people["chen"]):
            assert client.get("/notifications/settings").json()["email_tickets"] is True

    def test_the_body_cannot_name_another_user(self, client, db_session, people, mail):
        with acting_as(people["student"]):
            r = client.put("/notifications/settings", json={"email_tickets": False, "user_id": people["chen"].id})
            assert r.status_code == 200
        with acting_as(people["chen"]):
            assert client.get("/notifications/settings").json()["email_tickets"] is True

    def test_a_non_boolean_is_rejected(self, client, people, mail):
        with acting_as(people["student"]):
            assert client.put("/notifications/settings", json={"email_tickets": "maybe"}).status_code == 422
            assert client.put("/notifications/settings", json={}).status_code == 422

    def test_a_token_with_no_account_cannot_write(self, client, mail):
        ghost = CurrentUser(
            id=str(uuid.uuid4()),
            email="g",
            display_name="G",
            role=UserRole.student,
            tenant_id=DEV_TENANT,
            keycloak_id="g",
        )
        with acting_as(ghost):
            assert client.put("/notifications/settings", json={"email_tickets": False}).status_code == 404
            assert client.get("/notifications/settings").json()["email_tickets"] is True


class TestBaseUrl:
    def test_prefers_web_base_url_then_the_installer_one_and_ignores_junk(self, monkeypatch):
        for k in ("WEB_BASE_URL", "LTI_WEB_BASE_URL"):
            monkeypatch.delenv(k, raising=False)
        assert ticket_email.web_base_url() == ""
        monkeypatch.setenv("LTI_WEB_BASE_URL", "https://lti.example.test/")
        assert ticket_email.web_base_url() == "https://lti.example.test"
        monkeypatch.setenv("WEB_BASE_URL", "javascript:alert(1)")
        assert ticket_email.web_base_url() == "https://lti.example.test"
        monkeypatch.setenv("WEB_BASE_URL", "https://tn.example.test")
        assert ticket_email.web_base_url() == "https://tn.example.test"

    def test_without_a_base_url_the_mail_has_no_link_but_still_tells_them_where(
        self, client, people, mail, monkeypatch
    ):
        monkeypatch.delenv("WEB_BASE_URL")
        monkeypatch.delenv("LTI_WEB_BASE_URL", raising=False)
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            client.patch(f"/tickets/{t['id']}", json={"status": "resolved"})
        assert "http" not in mail.sent[0][2] and "Support" in mail.sent[0][2]


class TestThrottle:
    def test_after_five_minutes_the_next_event_mails_again(self, client, db_session, people, mail):
        t = _file(client, people["student"])
        with acting_as(people["chen"]):
            client.post(f"/tickets/{t['id']}/comments", json={"body": "one"})
        _age_notifications(db_session)
        with acting_as(people["chen"]):
            client.post(f"/tickets/{t['id']}/comments", json={"body": "two"})
        assert len(mail.sent) == 2

    def test_another_ticket_is_not_throttled_by_this_one(self, client, people, mail):
        a = _file(client, people["student"])
        b = _file(client, people["student"])
        with acting_as(people["chen"]):
            client.post(f"/tickets/{a['id']}/comments", json={"body": "one"})
            client.post(f"/tickets/{b['id']}/comments", json={"body": "one"})
        assert len(mail.sent) == 2
