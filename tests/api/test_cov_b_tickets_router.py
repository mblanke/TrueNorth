# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""The support queue and assignee endpoints of trouble tickets
(app/routers/tickets.py: ``list_assignees``, ``create_queue``, ``delete_queue``).

Assignees are the tenant's staff, by display name; whoever calls is offered too, so a
Student triaging their own ticket can hand it back to themselves. Queues: the slug is the
API-facing name and must look like one; a queue holding tickets is not deletable; and the
tenant always keeps one queue, because every new ticket needs somewhere to go — deleting
the default promotes the oldest survivor instead.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import User, UserRole

DEV_TENANT = "00000000-0000-0000-0000-000000000001"


def _who(role: UserRole, tenant: str = DEV_TENANT, display_name: str | None = None) -> CurrentUser:
    return CurrentUser(
        id=str(uuid.uuid4()),
        email=f"{role.value}-{uuid.uuid4().hex[:6]}@example.test",
        display_name=display_name or f"{role.value} person",
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


def _staff_row(db_session, who: CurrentUser) -> None:
    db_session.add(
        User(
            id=uuid.UUID(who.id),
            keycloak_id=who.keycloak_id,
            email=who.email,
            display_name=who.display_name,
            role=who.role,
            tenant_id=uuid.UUID(who.tenant_id),
        )
    )
    db_session.commit()


# ── GET /tickets/assignees ─────────────────────────────────────────────
def test_assignees_are_the_tenant_s_staff_with_the_caller_offered_once(client, db_session):
    ada = _who(UserRole.instructor, display_name="Ada")
    sam = _who(UserRole.admin, display_name="Sam")
    _staff_row(db_session, ada)
    _staff_row(db_session, sam)
    with acting_as(ada):
        rows = client.get("/tickets/assignees").json()
    # The seeded dev admin is staff of this tenant too; sorted by display name.
    assert [(r["display_name"], r["role"]) for r in rows] == [
        ("Ada", "instructor"),
        ("Dev Admin", "admin"),
        ("Sam", "admin"),
    ]


def test_a_caller_who_is_not_staff_is_offered_at_the_front(client, db_session):
    ada = _who(UserRole.instructor, display_name="Ada")
    _staff_row(db_session, ada)
    ops = _who(UserRole.range_ops, display_name="Ops person")
    with acting_as(ops):
        rows = client.get("/tickets/assignees").json()
    assert rows[0]["display_name"] == "Ops person" and rows[0]["role"] == "range_ops"
    assert [r["display_name"] for r in rows[1:]] == ["Ada", "Dev Admin"]


def test_a_student_may_not_list_assignees(client):
    with acting_as(_who(UserRole.student)):
        assert client.get("/tickets/assignees").status_code == 403


# ── POST /tickets/queues ───────────────────────────────────────────────
@pytest.fixture
def admin(db_session):
    """An admin with a User row: queue and ticket writes audit ``user_id``, which is a FK."""
    who = _who(UserRole.admin)
    _staff_row(db_session, who)
    return who


@pytest.mark.parametrize("slug", ["Bad_Slug", "Support", "", "-leading-hyphen"])
def test_a_queue_slug_must_look_like_a_slug(client, admin, slug):
    with acting_as(admin):
        resp = client.post("/tickets/queues", json={"name": "Escalations", "slug": slug})
    assert resp.status_code == 422


def test_making_a_new_queue_the_default_retires_the_old_default(client, admin):
    with acting_as(admin):
        client.post("/tickets/queues", json={"name": "Support", "slug": "support", "is_default": True})
        second = client.post("/tickets/queues", json={"name": "Escalations", "slug": "escalations", "is_default": True})
        queues = {q["slug"]: q["is_default"] for q in client.get("/tickets/queues").json()}
    assert second.status_code == 201
    assert queues == {"support": False, "escalations": True}


# ── DELETE /tickets/queues/{id} ────────────────────────────────────────
def test_a_queue_holding_tickets_is_not_deletable(client, admin):
    with acting_as(admin):
        ticket = client.post("/tickets", json={"subject": "vm won't boot", "description": "stuck at BIOS"}).json()
        resp = client.delete(f"/tickets/queues/{ticket['queue_id']}")
    assert resp.status_code == 409
    assert "Move this queue's tickets elsewhere" in resp.json()["detail"]


def test_the_last_queue_is_kept_and_the_default_delete_promotes_the_oldest(client, admin):
    with acting_as(admin):
        only = client.post("/tickets/queues", json={"name": "Only", "slug": "only", "is_default": True}).json()
        resp = client.delete(f"/tickets/queues/{only['id']}")
        assert resp.status_code == 409
        assert "Keep at least one queue" in resp.json()["detail"]
        # A second queue makes the first deletable — and it is the default, so the oldest
        # survivor is promoted in its place.
        client.post("/tickets/queues", json={"name": "Escalations", "slug": "escalations"})
        assert client.delete(f"/tickets/queues/{only['id']}").status_code == 204
        queues = {q["slug"]: q["is_default"] for q in client.get("/tickets/queues").json()}
    assert queues == {"escalations": True}
