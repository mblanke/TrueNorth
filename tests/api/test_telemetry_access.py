"""Who may write to and read a range's telemetry index.

`POST /telemetry/{range_id}/events` used to need only a login: any user could write
events into any range's index, in any tenant. Detection objectives are scored against
that index, so a Student could award themselves points. `GET .../search` likewise read
any tenant's telemetry. Both now require the range to belong to the caller's tenant, and
writing needs `telemetry:write` (not held by Students or observers).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from app import main as app_main
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Range, Template, UserRole

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"
EVENT = [{"ParentImage": "C:\\WINWORD.EXE", "Image": "powershell.exe"}]


@contextmanager
def acting_as(role: UserRole, tenant: str = DEV_TENANT):
    who = CurrentUser(id=str(uuid.uuid4()), email=f"{role.value}@example.test", display_name=role.value,
                      role=role, tenant_id=tenant, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


class _Backend:
    def __init__(self):
        self.ingested: list[tuple[str, list[dict]]] = []
        self.searched: list[str] = []

    async def ingest(self, index, events):
        self.ingested.append((index, events))
        return len(events)

    async def search(self, index, query, size):
        self.searched.append(index)
        return {"hits": {"hits": []}}


@pytest.fixture
def backend(monkeypatch):
    b = _Backend()
    monkeypatch.setattr(app_main, "get_search_backend", lambda: b)
    return b


def _range(db, tenant: str) -> Range:
    t = Template(name=f"t-{uuid.uuid4().hex[:6]}", version="1.0", yaml="name: t\nnodes: []\n",
                 tenant_id=uuid.UUID(tenant))
    db.add(t)
    db.flush()
    r = Range(name=f"r-{uuid.uuid4().hex[:6]}", template_id=t.id, tenant_id=uuid.UUID(tenant))
    db.add(r)
    db.flush()
    return r


class TestIngest:
    @pytest.mark.parametrize("role", [UserRole.instructor, UserRole.range_ops, UserRole.admin])
    def test_staff_can_write_to_their_tenants_range(self, client, db_session, backend, role):
        r = _range(db_session, DEV_TENANT)
        with acting_as(role):
            resp = client.post(f"/telemetry/{r.id}/events", json=EVENT)
        assert resp.status_code == 202, resp.text
        assert resp.json() == {"accepted": 1}
        index, events = backend.ingested[0]
        assert index == f"range-{r.id}"
        assert events[0]["tenant_id"] == DEV_TENANT and events[0]["range_id"] == str(r.id)

    @pytest.mark.parametrize("role", [UserRole.student, UserRole.observer])
    def test_students_and_observers_cannot_write(self, client, db_session, backend, role):
        r = _range(db_session, DEV_TENANT)
        with acting_as(role):
            resp = client.post(f"/telemetry/{r.id}/events", json=EVENT)
        assert resp.status_code == 403
        assert backend.ingested == []

    def test_cannot_write_to_another_tenants_range(self, client, db_session, backend):
        foreign = _range(db_session, OTHER_TENANT)
        with acting_as(UserRole.admin):  # even an admin is scoped to their own tenant
            resp = client.post(f"/telemetry/{foreign.id}/events", json=EVENT)
        assert resp.status_code == 404
        assert backend.ingested == []

    def test_unknown_range_is_404_not_a_fresh_index(self, client, backend):
        with acting_as(UserRole.instructor):
            resp = client.post(f"/telemetry/{uuid.uuid4()}/events", json=EVENT)
        assert resp.status_code == 404
        assert backend.ingested == []


class TestSearch:
    def test_student_can_search_their_tenants_range(self, client, db_session, backend):
        r = _range(db_session, DEV_TENANT)
        with acting_as(UserRole.student):
            resp = client.get(f"/telemetry/{r.id}/search", params={"q": "*"})
        assert resp.status_code == 200, resp.text
        assert backend.searched == [f"range-{r.id}"]

    def test_cannot_read_another_tenants_telemetry(self, client, db_session, backend):
        foreign = _range(db_session, OTHER_TENANT)
        with acting_as(UserRole.instructor):
            resp = client.get(f"/telemetry/{foreign.id}/search", params={"q": "*"})
        assert resp.status_code == 404
        assert backend.searched == []
