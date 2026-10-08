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
    def test_staff_can_search_their_tenants_range(self, client, db_session, backend):
        r = _range(db_session, DEV_TENANT)
        with acting_as(UserRole.observer):
            resp = client.get(f"/telemetry/{r.id}/search", params={"q": "*"})
        assert resp.status_code == 200, resp.text
        assert backend.searched == [f"range-{r.id}"]

    def test_a_student_cannot_search_a_range_they_are_not_on(self, client, db_session, backend):
        """Security sweep M2: tenant membership alone is not enough for a Student
        (tests/api/test_telemetry_participation.py has the cases that are)."""
        r = _range(db_session, DEV_TENANT)
        with acting_as(UserRole.student):
            resp = client.get(f"/telemetry/{r.id}/search", params={"q": "*"})
        assert resp.status_code == 404
        assert backend.searched == []

    def test_cannot_read_another_tenants_telemetry(self, client, db_session, backend):
        foreign = _range(db_session, OTHER_TENANT)
        with acting_as(UserRole.instructor):
            resp = client.get(f"/telemetry/{foreign.id}/search", params={"q": "*"})
        assert resp.status_code == 404
        assert backend.searched == []

    @pytest.mark.parametrize("q", ["_index:range-*", "cmd:*admin", 'a:"open', "a:x OR a:y"])
    def test_query_outside_the_grammar_is_422_and_never_reaches_the_backend(self, client, db_session, backend, q):
        r = _range(db_session, DEV_TENANT)
        with acting_as(UserRole.instructor):
            resp = client.get(f"/telemetry/{r.id}/search", params={"q": q})
        assert resp.status_code == 422, resp.text
        assert "Invalid search query" in resp.json()["detail"]
        assert backend.searched == []

    def test_overlong_query_is_422(self, client, db_session, backend):
        r = _range(db_session, DEV_TENANT)
        with acting_as(UserRole.instructor):
            resp = client.get(f"/telemetry/{r.id}/search", params={"q": "x" * 513})
        assert resp.status_code == 422
        assert backend.searched == []

    def test_ownership_is_checked_before_the_query(self, client, db_session, backend):
        """A foreign range is 404 whatever the query, so q cannot probe for range ids."""
        foreign = _range(db_session, OTHER_TENANT)
        with acting_as(UserRole.instructor):
            resp = client.get(f"/telemetry/{foreign.id}/search", params={"q": "_index:x"})
        assert resp.status_code == 404


class TestSearchANeverIngestedRange:
    """Through the real OpenSearch backend: a range with no events yet has no index."""

    @pytest.fixture
    def opensearch(self, monkeypatch):
        from app.search_backends import OpenSearchBackend

        b = OpenSearchBackend(url="http://mock-os:9200")
        monkeypatch.setattr(app_main, "get_search_backend", lambda: b)
        return b

    def test_index_not_found_is_an_empty_page_not_502(self, client, db_session, opensearch, respx_mock):
        import httpx

        r = _range(db_session, DEV_TENANT)
        respx_mock.post(f"http://mock-os:9200/range-{r.id}/_search").mock(
            return_value=httpx.Response(
                404,
                json={"error": {"type": "index_not_found_exception", "reason": "no such index"}, "status": 404},
            )
        )
        with acting_as(UserRole.instructor):
            resp = client.get(f"/telemetry/{r.id}/search", params={"q": "*"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["hits"]["hits"] == []
        assert resp.json()["hits"]["total"]["value"] == 0

    def test_a_backend_outage_is_still_502(self, client, db_session, opensearch, respx_mock):
        import httpx

        r = _range(db_session, DEV_TENANT)
        respx_mock.post(f"http://mock-os:9200/range-{r.id}/_search").mock(
            return_value=httpx.Response(503, json={"error": {"type": "cluster_block_exception"}})
        )
        with acting_as(UserRole.instructor):
            resp = client.get(f"/telemetry/{r.id}/search", params={"q": "*"})
        assert resp.status_code == 502


class TestMitreTagging:
    def _ingest(self, client, db_session, backend, events):
        r = _range(db_session, DEV_TENANT)
        with acting_as(UserRole.instructor):
            resp = client.post(f"/telemetry/{r.id}/events", json=events)
        assert resp.status_code == 202, resp.text
        return backend.ingested[0][1]

    def test_event_type_maps_to_a_technique(self, client, db_session, backend):
        [ev] = self._ingest(client, db_session, backend, [{"event_type": "process_exec"}])
        assert ev["mitre_technique"] == ["T1059"]

    def test_inject_technique_id_wins_over_event_type(self, client, db_session, backend):
        [ev] = self._ingest(
            client, db_session, backend, [{"event_type": "process_exec", "technique_id": "t1003.001"}]
        )
        assert ev["mitre_technique"] == ["T1003.001"]

    def test_unknown_event_is_left_untagged(self, client, db_session, backend):
        [ev] = self._ingest(client, db_session, backend, EVENT)
        assert "mitre_technique" not in ev
