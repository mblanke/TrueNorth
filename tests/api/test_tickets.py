"""Trouble tickets: filing, triage, the board, comments, attachments, history.

A student sees only tickets they reported and never internal notes. Anyone else's
ticket — or another tenant's — is a 404, never a 403.
"""

import io
import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Range, Template, Ticket, User, UserRole

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


def _who(role: UserRole, tenant: str = DEV_TENANT, user_id: uuid.UUID | None = None) -> CurrentUser:
    return CurrentUser(
        id=str(user_id or uuid.uuid4()),
        email=f"{role.value}-{uuid.uuid4().hex[:6]}@example.test",
        display_name=f"{role.value} person",
        role=role,
        tenant_id=tenant,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )


@contextmanager
def acting_as(who: CurrentUser):
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def student():
    return _who(UserRole.student)


@pytest.fixture
def other_student():
    return _who(UserRole.student)


@pytest.fixture
def instructor(db_session):
    who = _who(UserRole.instructor)
    db_session.add(
        User(
            id=uuid.UUID(who.id),
            keycloak_id=who.keycloak_id,
            email=who.email,
            display_name=who.display_name,
            role=UserRole.instructor,
            tenant_id=uuid.UUID(DEV_TENANT),
        )
    )
    db_session.commit()
    return who


@pytest.fixture
def rng(db_session):
    tpl = Template(name="tkt-tpl", version="1.0", yaml="nodes: []", tenant_id=DEV_TENANT)
    db_session.add(tpl)
    db_session.flush()
    r = Range(name="DP2 AD Lab", template_id=tpl.id, tenant_id=DEV_TENANT, state="ready")
    db_session.add(r)
    db_session.commit()
    return r


def _file(client, subject="VM won't boot", **extra):
    resp = client.post("/tickets", json={"subject": subject, "description": "dc01 stuck at BIOS", **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestFiling:
    def test_student_files_a_ticket_against_a_range(self, client, student, rng):
        with acting_as(student):
            t = _file(client, range_id=str(rng.id))
        assert t["key"] == "TN-1"
        assert t["status"] == "open"
        assert t["range_name"] == "DP2 AD Lab"
        assert t["queue_name"] == "Support"  # default queue made on first use
        assert t["reporter_name"] == "student person"
        assert t["can_work"] is False

    def test_numbers_are_per_tenant(self, client, student):
        with acting_as(student):
            assert _file(client)["number"] == 1
            assert _file(client)["number"] == 2
        with acting_as(_who(UserRole.student, tenant=OTHER_TENANT)):
            assert _file(client)["number"] == 1

    def test_cannot_link_another_tenants_range(self, client, db_session):
        other = uuid.uuid4()
        tpl = Template(name="foreign", version="1.0", yaml="nodes: []", tenant_id=other)
        db_session.add(tpl)
        db_session.flush()
        r = Range(name="Foreign", template_id=tpl.id, tenant_id=other, state="ready")
        db_session.add(r)
        db_session.commit()
        resp = client.post("/tickets", json={"subject": "x", "range_id": str(r.id)})
        assert resp.status_code == 404

    def test_student_cannot_assign(self, client, student, instructor):
        with acting_as(student):
            resp = client.post("/tickets", json={"subject": "x", "assignee_id": instructor.id})
        assert resp.status_code == 403


class TestVisibility:
    def test_student_sees_only_their_own(self, client, student, other_student):
        with acting_as(student):
            mine = _file(client, subject="mine")
        with acting_as(other_student):
            _file(client, subject="theirs")
            assert [t["subject"] for t in client.get("/tickets?scope=all").json()] == ["theirs"]
            assert client.get(f"/tickets/{mine['id']}").status_code == 404
            assert client.post(f"/tickets/{mine['id']}/comments", json={"body": "hi"}).status_code == 404
            assert client.get(f"/tickets/{mine['id']}/activity").status_code == 404

    def test_other_tenant_staff_cannot_reach_it(self, client, student):
        with acting_as(student):
            t = _file(client)
        with acting_as(_who(UserRole.admin, tenant=OTHER_TENANT)):
            assert client.get(f"/tickets/{t['id']}").status_code == 404
            assert client.patch(f"/tickets/{t['id']}", json={"status": "closed"}).status_code == 404
            assert client.get("/tickets").json() == []
            assert all(c["tickets"] == [] for c in client.get("/tickets/board").json())

    def test_staff_see_every_ticket_and_can_filter(self, client, student, instructor):
        with acting_as(student):
            _file(client, subject="a")
            _file(client, subject="b", priority="critical")
        with acting_as(instructor):
            assert len(client.get("/tickets").json()) == 2
            assert [t["subject"] for t in client.get("/tickets?priority=critical").json()] == ["b"]
            assert [t["subject"] for t in client.get("/tickets?q=TN-1").json()] == ["a"]
            assert client.get("/tickets?scope=mine").json() == []


class TestComments:
    def test_internal_notes_are_staff_only(self, client, student, instructor):
        with acting_as(student):
            t = _file(client)
            assert client.post(f"/tickets/{t['id']}/comments", json={"body": "x", "is_internal": True}).status_code == 403
        with acting_as(instructor):
            client.post(f"/tickets/{t['id']}/comments", json={"body": "reboot it", "is_internal": False})
            client.post(f"/tickets/{t['id']}/comments", json={"body": "user error lol", "is_internal": True})
            assert len(client.get(f"/tickets/{t['id']}/comments").json()) == 2
        with acting_as(student):
            bodies = [c["body"] for c in client.get(f"/tickets/{t['id']}/comments").json()]
        assert bodies == ["reboot it"]

    def test_reporter_reply_takes_a_waiting_ticket_back_to_open(self, client, student, instructor):
        with acting_as(student):
            t = _file(client)
        with acting_as(instructor):
            client.patch(f"/tickets/{t['id']}", json={"status": "waiting"})
        with acting_as(student):
            client.post(f"/tickets/{t['id']}/comments", json={"body": "tried that, still broken"})
            assert client.get(f"/tickets/{t['id']}").json()["status"] == "open"


class TestTriage:
    def test_staff_assign_and_every_change_is_logged(self, client, student, instructor):
        with acting_as(student):
            t = _file(client)
        with acting_as(instructor):
            r = client.patch(
                f"/tickets/{t['id']}", json={"assignee_id": instructor.id, "priority": "high", "status": "in_progress"}
            )
            assert r.status_code == 200
            assert r.json()["assignee_name"] == "instructor person"
            assert [x["subject"] for x in client.get("/tickets?scope=assigned").json()] == [t["subject"]]
            fields = [a["field"] for a in client.get(f"/tickets/{t['id']}/activity").json()]
        assert fields[0] == "created"
        assert {"assignee_id", "priority", "status"} <= set(fields)

    def test_assignee_must_be_staff_in_the_tenant(self, client, student, instructor):
        with acting_as(student):
            t = _file(client)
        with acting_as(instructor):
            r = client.patch(f"/tickets/{t['id']}", json={"assignee_id": str(uuid.uuid4())})
        assert r.status_code == 422

    def test_reporter_may_only_edit_text_close_or_reopen(self, client, student, instructor):
        with acting_as(student):
            t = _file(client)
            assert client.patch(f"/tickets/{t['id']}", json={"priority": "critical"}).status_code == 403
            assert client.patch(f"/tickets/{t['id']}", json={"status": "resolved"}).status_code == 403
            assert client.patch(f"/tickets/{t['id']}", json={"subject": "VM dc01 won't boot"}).status_code == 200
        with acting_as(instructor):
            resolved = client.patch(f"/tickets/{t['id']}", json={"status": "resolved"}).json()
            assert resolved["resolved_at"] is not None
        with acting_as(student):
            closed = client.patch(f"/tickets/{t['id']}", json={"status": "closed"}).json()
            assert closed["closed_at"] is not None
            reopened = client.patch(f"/tickets/{t['id']}", json={"status": "open"}).json()
            assert reopened["closed_at"] is None and reopened["resolved_at"] is None

    def test_board_groups_by_status_and_move_logs(self, client, student, instructor):
        with acting_as(student):
            t = _file(client)
            assert client.get("/tickets/board").status_code == 403
        with acting_as(instructor):
            cols = {c["status"]: c["tickets"] for c in client.get("/tickets/board").json()}
            assert list(cols) == ["open", "in_progress", "waiting", "resolved", "closed"]
            assert [c["id"] for c in cols["open"]] == [t["id"]]
            moved = client.post(f"/tickets/{t['id']}/move", json={"status": "in_progress", "board_order": 1.5})
            assert moved.status_code == 200 and moved.json()["status"] == "in_progress"
            cols = {c["status"]: c["tickets"] for c in client.get("/tickets/board").json()}
            assert cols["open"] == [] and cols["in_progress"][0]["board_order"] == 1.5
            activity = client.get(f"/tickets/{t['id']}/activity").json()
        assert any(a["field"] == "status" and a["new_value"] == "in_progress" for a in activity)

    def test_only_admin_deletes(self, client, student, instructor, db_session):
        with acting_as(student):
            t = _file(client)
        with acting_as(instructor):
            assert client.delete(f"/tickets/{t['id']}").status_code == 403
        assert client.delete(f"/tickets/{t['id']}").status_code == 204  # dev admin
        assert client.get(f"/tickets/{t['id']}").status_code == 404
        assert db_session.query(Ticket).filter_by(id=uuid.UUID(t["id"])).one().deleted_at is not None


class TestQueues:
    def test_admin_manages_queues_and_one_default(self, client):
        a = client.post("/tickets/queues", json={"name": "Range", "slug": "range", "is_default": True}).json()
        client.post("/tickets/queues", json={"name": "Platform", "slug": "platform", "is_default": True})
        queues = {q["slug"]: q for q in client.get("/tickets/queues").json()}
        assert queues["platform"]["is_default"] and not queues["range"]["is_default"]
        assert client.delete(f"/tickets/queues/{a['id']}").status_code == 204

    def test_queue_in_use_cannot_be_deleted(self, client):
        q = client.post("/tickets/queues", json={"name": "Range", "slug": "range"}).json()
        _file(client, queue_id=q["id"])
        assert client.delete(f"/tickets/queues/{q['id']}").status_code == 409

    def test_instructor_cannot_create_queues(self, client, instructor):
        with acting_as(instructor):
            assert client.post("/tickets/queues", json={"name": "x", "slug": "x"}).status_code == 403


class TestAttachments:
    @pytest.fixture(autouse=True)
    def fake_store(self, monkeypatch):
        from app import object_store

        store: dict[str, bytes] = {}
        monkeypatch.setattr(object_store, "put_object", lambda k, d, c, bucket=None: store.__setitem__(k, d))
        monkeypatch.setattr(object_store, "get_object", lambda k, bucket=None: store[k])
        monkeypatch.setattr(object_store, "delete_object", lambda k, bucket=None: store.pop(k, None))
        return store

    def test_upload_download_delete(self, client, student, other_student, fake_store):
        with acting_as(student):
            t = _file(client)
            up = client.post(
                f"/tickets/{t['id']}/attachments",
                files=[("files", ("screen.html", io.BytesIO(b"<script>alert(1)</script>"), "text/html"))],
            )
            assert up.status_code == 201
            att = up.json()[0]
            dl = client.get(f"/tickets/attachments/{att['id']}")
            assert dl.status_code == 200 and dl.content == b"<script>alert(1)</script>"
            # Never rendered inline, whatever the uploader claimed it was.
            assert dl.headers["content-type"] == "application/octet-stream"
            assert dl.headers["content-disposition"].startswith("attachment")
        with acting_as(other_student):
            assert client.get(f"/tickets/attachments/{att['id']}").status_code == 404
            assert client.delete(f"/tickets/attachments/{att['id']}").status_code == 404
        with acting_as(student):
            assert client.delete(f"/tickets/attachments/{att['id']}").status_code == 204
            assert client.get(f"/tickets/{t['id']}/attachments").json() == []
        assert fake_store == {}

    def test_empty_file_rejected(self, client, student):
        with acting_as(student):
            t = _file(client)
            r = client.post(f"/tickets/{t['id']}/attachments", files=[("files", ("a.txt", io.BytesIO(b""), "text/plain"))])
        assert r.status_code == 422
