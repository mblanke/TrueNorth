"""Ops center: another tenant's exercise is not found, for every endpoint.

Create-annotation, inject and stats resolved the exercise through ``get_owned``. Listing
and deleting annotations, and listing and sharing commands, did not: given another
tenant's exercise id, a user read its analysts' notes and commands, deleted its notes,
and posted a command into it, which was also broadcast on its ``exercise.<id>`` channel
(security review of #32, 2026-10-05; carried from PR #36). Another tenant's objects are
404, never 403 (app/tenancy.py).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import AnalystAnnotation, Exercise, Range, Scenario, SharedCommand, Template, Tenant, User, UserRole

THEIRS = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
MINE = uuid.UUID("00000000-0000-0000-0000-000000000001")  # the AUTH_DISABLED user's tenant


def _exercise(db, tenant: uuid.UUID) -> Exercise:
    if not db.get(Tenant, tenant):
        db.add(Tenant(id=tenant, name=f"t-{tenant.hex[-4:]}", slug=f"t-{tenant.hex[-4:]}"))
        db.flush()
    t = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=tenant)
    s = Scenario(id=uuid.uuid4(), name="s", yaml="id: s\n", tenant_id=tenant)
    db.add_all([t, s])
    db.flush()
    r = Range(id=uuid.uuid4(), name="r", template_id=t.id, tenant_id=tenant)
    db.add(r)
    db.flush()
    ex = Exercise(
        id=uuid.uuid4(), name="e", range_id=r.id, scenario_id=s.id, tenant_id=tenant, started_at=datetime.now(UTC)
    )
    db.add(ex)
    db.commit()
    return ex


def _user(db, user_id: uuid.UUID, tenant: uuid.UUID, name: str) -> User:
    """A users row: annotations and commands reference their author (keys are enforced)."""
    u = User(id=user_id, email=f"{user_id.hex[:8]}@example.test", display_name=name,
             role=UserRole.admin, tenant_id=tenant, keycloak_id=f"kc-{user_id.hex[:8]}")
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def theirs(db_session):
    ex = _exercise(db_session, THEIRS)
    analyst = _user(db_session, uuid.uuid4(), THEIRS, "their analyst")
    note = AnalystAnnotation(
        id=uuid.uuid4(),
        exercise_id=ex.id,
        user_id=analyst.id,
        user_display_name="their analyst",
        content="their finding",
        annotation_type="note",
        severity="info",
        tags="[]",
        created_at=datetime.now(UTC),
    )
    cmd = SharedCommand(
        id=uuid.uuid4(),
        exercise_id=ex.id,
        user_id=analyst.id,
        user_display_name="their analyst",
        command="nmap -sV 10.0.0.0/24",
        shared_at=datetime.now(UTC),
    )
    db_session.add_all([note, cmd])
    db_session.commit()
    return ex, note, cmd


@pytest.fixture
def broadcasts(monkeypatch):
    sent = []

    async def broadcast(channel, message_type, data):
        sent.append((channel, message_type))

    monkeypatch.setattr(fastapi_app.state.ws_manager, "broadcast", broadcast)
    return sent


def test_their_annotations_are_not_listed(client, theirs):
    ex, _, _ = theirs
    r = client.get(f"/ops/exercises/{ex.id}/annotations")
    assert r.status_code == 404, r.text


def test_their_annotation_cannot_be_deleted(client, db_session, theirs):
    ex, note, _ = theirs
    assert client.delete(f"/ops/exercises/{ex.id}/annotations/{note.id}").status_code == 404
    db_session.expire_all()
    assert db_session.get(AnalystAnnotation, note.id) is not None


def test_their_commands_are_not_listed(client, theirs):
    ex, _, _ = theirs
    assert client.get(f"/ops/exercises/{ex.id}/commands").status_code == 404


def test_no_command_can_be_shared_into_their_exercise(client, db_session, theirs, broadcasts):
    ex, _, _ = theirs
    r = client.post(f"/ops/exercises/{ex.id}/commands", json={"command": "rm -rf /"})
    assert r.status_code == 404, r.text
    db_session.expire_all()
    assert db_session.query(SharedCommand).filter(SharedCommand.exercise_id == ex.id).count() == 1
    assert broadcasts == []


def test_my_own_exercise_still_works(client, db_session, broadcasts):
    ex = _exercise(db_session, MINE)
    assert client.post(f"/ops/exercises/{ex.id}/commands", json={"command": "whoami"}).status_code == 201
    assert len(client.get(f"/ops/exercises/{ex.id}/commands").json()) == 1
    assert client.get(f"/ops/exercises/{ex.id}/annotations").status_code == 200
    assert broadcasts == [(f"exercise.{ex.id}", "command_shared")]


@contextmanager
def _nameless_user():
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email="n@example.test",
        display_name="",
        role=UserRole.admin,
        tenant_id=str(MINE),
        keycloak_id="kc-n",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def test_a_user_without_a_display_name_can_post(client, db_session, broadcasts):
    """``user.display_name or user.username``: CurrentUser has no ``username`` (500)."""
    ex = _exercise(db_session, MINE)
    with _nameless_user() as who:
        _user(db_session, uuid.UUID(who.id), MINE, "")
        r = client.post(f"/ops/exercises/{ex.id}/commands", json={"command": "id"})
        assert r.status_code == 201, r.text
        assert r.json()["user_display_name"] == "n@example.test"
        a = client.post(f"/ops/exercises/{ex.id}/annotations", json={"content": "x"})
        assert a.status_code == 201, a.text
