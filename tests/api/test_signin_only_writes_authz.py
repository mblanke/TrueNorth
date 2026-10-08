"""Endpoints that were "any signed-in user" and now check a permission.

Found by tests/api/test_permission_coverage_guard.py:

- ``/kits`` and ``/network-devices``: physical infrastructure — ``infra:read`` /
  ``infra:write`` (range ops, instructors read; range ops and admins write);
- ``/collective-exercises``: the MESL is exercise control, expected actions included —
  ``exercise:create`` for reads and writes;
- ``/ai/scenario-draft`` (``scenario:create``) and ``/ai/detection-draft``
  (``range:update``, the right that saves a detection rule).

A Student gets 403 on every one, before any row is touched or the AI is called.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    Exercise,
    ExerciseState,
    KitDefinition,
    NetworkDevice,
    Range,
    Template,
    Tenant,
    User,
    UserRole,
)
from app.routers import ai_authoring, exercises_collective

TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d001")


@pytest.fixture
def tenant(db_session):
    if db_session.get(Tenant, TENANT) is None:
        db_session.add(Tenant(id=TENANT, name="authz-d001", slug="authz-d001"))
    db_session.flush()


@pytest.fixture
def ai_calls(monkeypatch):
    calls: list[str] = []

    async def _proxy(path, payload):
        calls.append(path)
        return {"output": "draft", "model_used": "stub"}

    def _mesl_post(url, **kwargs):  # the MESL generator calls httpx.post directly
        calls.append(url)
        raise RuntimeError("no orchestrator in tests")

    monkeypatch.setattr(ai_authoring, "_proxy", _proxy)
    monkeypatch.setattr(exercises_collective.httpx, "post", _mesl_post)
    return calls


def _user(db, role: UserRole) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=TENANT,
    )
    db.add(u)
    db.flush()
    return u


@contextmanager
def acting_as(user: User):
    who = CurrentUser(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        tenant_id=str(user.tenant_id),
        keycloak_id=user.keycloak_id,
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _kit(db) -> KitDefinition:
    k = KitDefinition(name="Rack A", slug=f"rack-{uuid.uuid4().hex[:8]}", tenant_id=TENANT)
    db.add(k)
    db.flush()
    return k


def _device(db) -> NetworkDevice:
    d = NetworkDevice(name="core-sw1", vendor="Cisco", model="9300", management_ip="10.0.0.2", tenant_id=TENANT)
    db.add(d)
    db.flush()
    return d


def _collective(db) -> Exercise:
    tpl = Template(name=f"tpl-{uuid.uuid4().hex[:6]}", version="1.0", yaml="nodes: []", tenant_id=TENANT)
    db.add(tpl)
    db.flush()
    rng = Range(name="Blue range", template_id=tpl.id, tenant_id=TENANT)
    db.add(rng)
    db.flush()
    ex = Exercise(name="Cyber Shield", kind="collective", range_id=rng.id, state=ExerciseState.pending, tenant_id=TENANT)
    db.add(ex)
    db.flush()
    return ex


def _calls(db) -> list[tuple[str, str, dict]]:
    """(method, path, kwargs) for every newly gated endpoint, against real rows."""
    kit, dev, ex = _kit(db), _device(db), _collective(db)
    csv = {"files": {"file": ("x.csv", b"ref,text\nO1,Detect beaconing\n", "text/csv")}}
    return [
        ("get", "/kits/", {}),
        ("post", "/kits/", {"json": {"name": "Rack B", "slug": f"rack-{uuid.uuid4().hex[:8]}"}}),
        ("delete", f"/kits/{kit.id}", {}),
        ("get", "/network-devices/", {}),
        ("get", "/network-devices/summary", {}),
        ("post", "/network-devices/", {"json": {"name": "edge", "vendor": "Juniper", "model": "SRX", "management_ip": "10.0.0.3"}}),
        ("patch", f"/network-devices/{dev.id}", {"json": {"notes": "moved"}}),
        ("delete", f"/network-devices/{dev.id}", {}),
        ("get", "/collective-exercises", {}),
        ("get", f"/collective-exercises/{ex.id}", {}),
        ("post", "/collective-exercises", {"json": {"name": "Ex 2", "range_id": str(ex.range_id)}}),
        ("post", f"/collective-exercises/{ex.id}/objectives/import", csv),
        ("post", f"/collective-exercises/{ex.id}/mesl/import", csv),
        ("patch", f"/collective-exercises/{ex.id}/mesl/{uuid.uuid4()}", {"json": {"status": "staged"}}),
        ("post", f"/collective-exercises/{ex.id}/mesl/generate", {"json": {}}),
        ("post", "/ai/scenario-draft", {"json": {"objectives": ["Detect beaconing"]}}),
        ("post", "/ai/detection-draft", {"json": {"technique": "T1071"}}),
    ]


def test_student_gets_403_everywhere_and_nothing_changes(client, db_session, tenant, ai_calls):
    calls = _calls(db_session)
    before = (
        db_session.query(KitDefinition).count(),
        db_session.query(NetworkDevice).count(),
        db_session.query(Exercise).count(),
    )
    refused = []
    with acting_as(_user(db_session, UserRole.student)):
        for method, path, kwargs in calls:
            r = client.request(method.upper(), path, **kwargs)
            if r.status_code != 403:
                refused.append(f"{method.upper()} {path} -> {r.status_code}")
    assert not refused, "a Student reached:\n  " + "\n  ".join(refused)
    after = (
        db_session.query(KitDefinition).count(),
        db_session.query(NetworkDevice).count(),
        db_session.query(Exercise).count(),
    )
    assert after == before
    assert ai_calls == []


@pytest.mark.parametrize(
    ("role", "allowed_prefixes"),
    [
        # Instructors author exercises and drafts, and may see — not change — infrastructure.
        (UserRole.instructor, ("GET /kits", "GET /network-devices", "/collective-exercises", "/ai/")),
        # Range ops own infrastructure and detection rules, not exercises or scenarios.
        (UserRole.range_ops, ("/kits", "/network-devices", "POST /ai/detection-draft")),
        (UserRole.admin, ("/",)),
    ],
)
def test_staff_reach_exactly_what_their_role_grants(client, db_session, tenant, ai_calls, role, allowed_prefixes):
    calls = _calls(db_session)
    wrong = []
    with acting_as(_user(db_session, role)):
        for method, path, kwargs in calls:
            key = f"{method.upper()} {path}"
            allowed = any(p in key for p in allowed_prefixes)
            r = client.request(method.upper(), path, **kwargs)
            if allowed and r.status_code == 403:
                wrong.append(f"{key}: 403 but {role.value} should be allowed")
            if not allowed and r.status_code != 403:
                wrong.append(f"{key}: {r.status_code} but {role.value} should be refused")
    assert not wrong, "\n".join(wrong)
