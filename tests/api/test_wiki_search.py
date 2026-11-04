"""Wiki search through the search backend (#380): ranking from OpenSearch, visibility from the database.

The test that matters most: whatever ids the backend returns, nobody sees a page they may not
read. The index carries no visibility, so it can be stale or wrong and still reveal nothing.
"""

import json
import time
import uuid
from contextlib import contextmanager

import httpx
import pytest
from app import wiki_search
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Tenant, User, UserRole
from app.models_wiki import WikiPage, WikiSpace
from app.search_backends import opensearch
from app.search_backends.base import BaseSearchBackend, SearchBackendError, SearchMatch

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


def _person(db, role: UserRole, tenant: str = DEV_TENANT) -> CurrentUser:
    uid = uuid.uuid4()
    if tenant != DEV_TENANT and not db.get(Tenant, uuid.UUID(tenant)):
        db.add(Tenant(id=uuid.UUID(tenant), name="other", slug="other"))
        db.flush()
    db.add(
        User(
            id=uid,
            keycloak_id=f"kc-{uid.hex}",
            email=f"{uid.hex[:8]}@example.test",
            display_name=role.value,
            role=role,
            tenant_id=uuid.UUID(tenant),
        )
    )
    db.commit()
    return CurrentUser(
        id=str(uid), email="x", display_name=role.value, role=role, tenant_id=tenant, keycloak_id=f"kc-{uid.hex}"
    )


@contextmanager
def acting_as(who: CurrentUser):
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


class FakeBackend(BaseSearchBackend):
    """Holds documents; `match` returns `forced` ids if set, else ids whose title/body contain the word."""

    supports_documents = True

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.versions: dict[str, int] = {}
        self.queries: list[dict] = []
        self.forced: list[str] | None = None
        self.fail = False
        self.delay = 0.0

    async def ingest(self, index, events):
        return 0

    async def search(self, index, query, size=50):
        return {}

    async def match(self, index, query, size=0):
        import asyncio

        self.queries.append(query)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise SearchBackendError("down")
        if self.forced is not None:
            return SearchMatch(total=len(self.forced), ids=self.forced[:size])
        word = query["bool"]["must"][0]["simple_query_string"]["query"].lower()
        tenant = query["bool"]["filter"][0]["term"]["tenant_id"]
        ids = [i for i, d in self.docs.items() if d["tenant_id"] == tenant and word in (d["title"] + d["body"]).lower()]
        return SearchMatch(total=len(ids), ids=ids[:size])

    async def health_check(self):
        return True

    async def upsert_documents(self, index, docs, *, settings=None, versions=None):
        if self.fail:
            raise SearchBackendError("down")
        for i, d in docs.items():
            if versions and versions.get(i, 0) <= self.versions.get(i, -1):
                continue  # a newer one is stored
            self.versions[i] = (versions or {}).get(i, 0)
            self.docs[i] = d
        return len(docs)

    async def delete_documents(self, index, ids, *, settings=None, versions=None):
        for i in ids:
            if versions and versions.get(i, 0) <= self.versions.get(i, -1):
                continue
            self.versions[i] = (versions or {}).get(i, 0)
            self.docs.pop(i, None)
        return len(ids)


@pytest.fixture
def backend(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setenv("WIKI_OPENSEARCH", "true")
    monkeypatch.setattr(wiki_search, "get_search_backend", lambda: fake)
    return fake


@pytest.fixture
def who(db_session):
    return {
        "admin": _person(db_session, UserRole.admin),
        "ops": _person(db_session, UserRole.range_ops),
        "student": _person(db_session, UserRole.student),
        "foreign": _person(db_session, UserRole.admin, OTHER_TENANT),
    }


def in_request_context(fn):
    """Run `fn` on a worker thread under a running event loop, as a route does, then let the
    background indexing tasks it spawned finish. (A bare session has no loop: indexing is skipped.)"""
    import anyio

    async def main():
        await anyio.to_thread.run_sync(fn)
        await anyio.sleep(0.3)

    anyio.run(main)


def _wait(cond, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@pytest.fixture
def pages(client, who):
    """A public space with two pages, a staff space with one, and an unpublished draft."""
    out = {}
    with acting_as(who["admin"]):
        client.post("/wiki/spaces", json={"name": "Help", "slug": "help", "visibility": "all"})
        client.post("/wiki/spaces", json={"name": "Ops", "slug": "ops", "visibility": "staff"})
        out["reset"] = client.post(
            "/wiki/spaces/help/pages", json={"title": "Reset a lab", "body": "Use the reset button"}
        ).json()
        out["vpn"] = client.post(
            "/wiki/spaces/help/pages", json={"title": "VPN setup", "body": "reset your token first"}
        ).json()
        out["secret"] = client.post(
            "/wiki/spaces/ops/pages", json={"title": "Answer key", "body": "reset password is hunter2"}
        ).json()
        out["draft"] = client.post(
            "/wiki/spaces/help/pages", json={"title": "Draft", "body": "reset notes", "is_published": False}
        ).json()
    return out


def _search(client, who, q="reset", **kw):
    with acting_as(who):
        r = client.get("/wiki/search", params={"q": q, **kw})
        assert r.status_code == 200, r.text
        return [h["page_id"] for h in r.json()]


class TestQuery:
    def test_it_is_scoped_to_the_tenant_and_never_uses_the_free_form_parser(self):
        tid = uuid.uuid4()
        q = wiki_search.build_query('reset "lab" NOT x', tid)
        assert q["bool"]["filter"] == [{"term": {"tenant_id": str(tid)}}]
        assert [next(iter(m)) for m in q["bool"]["must"]] == ["simple_query_string"]  # never "query_string"
        for banned in ("FUZZY", "NEAR", "SLOP"):
            assert banned not in q["bool"]["must"][0]["simple_query_string"]["flags"]

    def test_the_document_carries_no_visibility(self):
        page = WikiPage(tenant_id=uuid.uuid4(), title="t", body="b", tags="x", is_published=False)
        assert set(wiki_search.document(page)) == {"tenant_id", "title", "tags", "body"}


class TestRanking:
    def test_pages_the_backend_ranks_come_first_in_its_order(self, client, who, pages, backend):
        backend.forced = [pages["vpn"]["id"], pages["reset"]["id"]]
        got = _search(client, who["student"])
        assert got[:2] == [pages["vpn"]["id"], pages["reset"]["id"]]

    def test_substring_matches_the_backend_missed_still_appear_after_them(self, client, who, pages, backend):
        backend.forced = [pages["vpn"]["id"]]
        got = _search(client, who["student"])
        assert got[0] == pages["vpn"]["id"] and pages["reset"]["id"] in got and len(got) == len(set(got))

    def test_it_is_asked_with_the_callers_tenant(self, client, who, pages, backend):
        _search(client, who["student"])
        assert backend.queries[-1]["bool"]["filter"][0]["term"]["tenant_id"] == DEV_TENANT


class TestVisibilityIsTheDatabases:
    def test_a_student_never_sees_staff_draft_other_tenant_or_deleted_pages_whatever_the_index_says(
        self, client, db_session, who, pages, backend
    ):
        with acting_as(who["admin"]):
            gone = client.post("/wiki/spaces/help/pages", json={"title": "Removed", "body": "reset"}).json()
            client.delete(f"/wiki/pages/{gone['id']}")
        foreign = _person(db_session, UserRole.admin, OTHER_TENANT)
        with acting_as(foreign):
            client.post("/wiki/spaces", json={"name": "F", "slug": "f", "visibility": "all"})
            other = client.post("/wiki/spaces/f/pages", json={"title": "Reset theirs", "body": "reset"}).json()
        backend.forced = [pages[k]["id"] for k in ("secret", "draft")] + [gone["id"], other["id"], str(uuid.uuid4())]
        got = _search(client, who["student"])
        assert set(got) == {pages["reset"]["id"], pages["vpn"]["id"]}  # only the substring matches they may read

    def test_staff_see_staff_pages_and_drafts_but_never_another_tenants(self, client, db_session, who, pages, backend):
        backend.forced = [pages["secret"]["id"], pages["draft"]["id"]]
        got = _search(client, who["ops"])
        assert pages["secret"]["id"] in got and pages["draft"]["id"] in got
        assert _search(client, who["foreign"]) == []

    def test_an_archived_space_hides_its_pages_from_the_ranking_too(self, client, db_session, who, pages, backend):
        db_session.query(WikiSpace).filter(WikiSpace.slug == "help").update({"is_archived": True})
        db_session.commit()
        backend.forced = [pages["reset"]["id"]]
        assert _search(client, who["ops"]) == [pages["secret"]["id"]]  # the staff space is not archived; "help" is


class TestFallback:
    def test_with_no_backend_it_is_the_old_search(self, client, who, pages, monkeypatch):
        monkeypatch.setenv("WIKI_OPENSEARCH", "false")
        got = _search(client, who["student"])
        assert set(got) == {pages["reset"]["id"], pages["vpn"]["id"]}

    def test_a_backend_that_errors_or_hangs_costs_nothing(self, client, who, pages, backend, monkeypatch):
        backend.fail = True
        assert set(_search(client, who["student"])) == {pages["reset"]["id"], pages["vpn"]["id"]}
        backend.fail = False
        backend.delay = 0.5
        monkeypatch.setattr(wiki_search, "QUERY_TIMEOUT_S", 0.05)
        assert set(_search(client, who["student"])) == {pages["reset"]["id"], pages["vpn"]["id"]}

    def test_a_backend_that_cannot_hold_documents_is_not_asked(self, client, who, pages, monkeypatch):
        class Plain(FakeBackend):
            supports_documents = False

        plain = Plain()
        monkeypatch.setenv("WIKI_OPENSEARCH", "true")
        monkeypatch.setattr(wiki_search, "get_search_backend", lambda: plain)
        _search(client, who["student"])
        assert plain.queries == []

    def test_it_is_off_unless_opensearch_is_configured(self, monkeypatch):
        for k in ("WIKI_OPENSEARCH", "OPENSEARCH_URL", "SEARCH_BACKEND"):
            monkeypatch.delenv(k, raising=False)
        assert wiki_search.enabled() is False
        monkeypatch.setenv("OPENSEARCH_URL", "https://opensearch:9200")
        assert wiki_search.enabled() is True
        monkeypatch.setenv("SEARCH_BACKEND", "null")
        assert wiki_search.enabled() is False
        monkeypatch.setenv("WIKI_OPENSEARCH", "false")
        monkeypatch.setenv("SEARCH_BACKEND", "opensearch")
        assert wiki_search.enabled() is False


class TestIndexingFollowsTheDatabase:
    def test_a_new_page_is_indexed_after_the_commit(self, client, who, backend):
        with acting_as(who["admin"]):
            client.post("/wiki/spaces", json={"name": "Help", "slug": "help", "visibility": "all"})
            page = client.post("/wiki/spaces/help/pages", json={"title": "Lab guide", "body": "alpha"}).json()
        assert _wait(lambda: page["id"] in backend.docs)
        assert backend.docs[page["id"]]["title"] == "Lab guide" and backend.docs[page["id"]]["tenant_id"] == DEV_TENANT

    def test_an_edit_replaces_the_document_and_a_delete_removes_it(self, client, who, backend):
        with acting_as(who["admin"]):
            client.post("/wiki/spaces", json={"name": "Help", "slug": "help", "visibility": "all"})
            page = client.post("/wiki/spaces/help/pages", json={"title": "Lab guide", "body": "alpha"}).json()
            assert _wait(lambda: page["id"] in backend.docs)
            client.put(f"/wiki/pages/{page['id']}", json={"base_revision": page["revision_number"], "body": "beta"})
            assert _wait(lambda: backend.docs.get(page["id"], {}).get("body") == "beta")
            client.delete(f"/wiki/pages/{page['id']}")
            assert _wait(lambda: page["id"] not in backend.docs)

    def test_a_backend_that_is_down_never_fails_the_save(self, client, who, backend):
        backend.fail = True
        with acting_as(who["admin"]):
            client.post("/wiki/spaces", json={"name": "Help", "slug": "help", "visibility": "all"})
            r = client.post("/wiki/spaces/help/pages", json={"title": "Lab guide", "body": "alpha"})
        assert r.status_code == 201

    def test_a_committed_save_from_a_bare_session_is_indexed_when_there_is_a_loop(self, db_session, backend):
        space = WikiSpace(tenant_id=uuid.UUID(DEV_TENANT), name="S", slug="s0")
        db_session.add(space)
        db_session.commit()
        page = self._page(space, "control")

        def work():
            db_session.add(page)
            db_session.commit()

        in_request_context(work)
        assert str(page.id) in backend.docs  # the positive control for the two tests below

    @staticmethod
    def _page(space, slug):
        return WikiPage(
            tenant_id=uuid.UUID(DEV_TENANT),
            space_id=space.id,
            title=slug,
            slug=slug,
            body="x",
            author_id=uuid.uuid4(),
            last_editor_id=uuid.uuid4(),
        )

    def test_a_rolled_back_save_indexes_nothing(self, db_session, backend):
        space = WikiSpace(tenant_id=uuid.UUID(DEV_TENANT), name="S", slug="s")
        db_session.add(space)
        db_session.commit()
        page = self._page(space, "ghost")

        def work():
            db_session.add(page)
            db_session.flush()
            db_session.rollback()
            db_session.commit()

        in_request_context(work)
        assert str(page.id) not in backend.docs

    def test_a_savepoint_that_rolled_back_indexes_nothing_but_the_outer_work_is(self, db_session, backend):
        space = WikiSpace(tenant_id=uuid.UUID(DEV_TENANT), name="S", slug="s2")
        db_session.add(space)
        db_session.commit()
        kept, dropped = self._page(space, "kept"), self._page(space, "dropped")

        def work():
            db_session.add(kept)
            db_session.flush()
            nested = db_session.begin_nested()
            db_session.add(dropped)
            db_session.flush()
            nested.rollback()
            db_session.commit()

        in_request_context(work)
        assert str(kept.id) in backend.docs and str(dropped.id) not in backend.docs


class TestReviewFindings:
    """The adversarial review of #380: each of these was a real gap."""

    def test_an_older_write_arriving_late_cannot_replace_a_newer_one(self, backend):
        import asyncio

        pid = uuid.uuid4()
        new = {"tenant_id": DEV_TENANT, "title": "Bar", "tags": "", "body": ""}
        old = {"tenant_id": DEV_TENANT, "title": "Foo", "tags": "", "body": ""}
        asyncio.run(wiki_search.apply({pid: (new, 200)}))
        asyncio.run(wiki_search.apply({pid: (old, 100)}))  # the slower, earlier save lands last
        assert backend.docs[str(pid)]["title"] == "Bar"
        asyncio.run(wiki_search.apply({pid: (None, 100)}))  # nor can a stale delete remove it
        assert str(pid) in backend.docs

    def test_a_page_changed_by_a_core_update_is_recorded_for_indexing(self, db_session, backend):
        from sqlalchemy import update

        space = WikiSpace(tenant_id=uuid.UUID(DEV_TENANT), name="S", slug="gen")
        db_session.add(space)
        db_session.commit()
        page = TestIndexingFollowsTheDatabase._page(space, "old")

        def create():
            db_session.add(page)
            db_session.commit()

        in_request_context(create)
        assert backend.docs[str(page.id)]["title"] == "old"

        def core_update():
            db_session.execute(
                update(WikiPage)
                .where(WikiPage.id == page.id)
                .values(title="New")
                .execution_options(synchronize_session=False)
            )
            db_session.refresh(page)
            wiki_search.record(db_session, page)  # what range_credentials/wiki_doc.py now does after its UPDATE
            db_session.commit()

        in_request_context(core_update)
        assert backend.docs[str(page.id)]["title"] == "New"

    def test_hidden_pages_do_not_use_up_the_ranking_for_students(self, client, who, pages, backend):
        asked = []
        real = wiki_search.ranked_ids
        import pytest as _pt  # noqa: F401

        def spy(q, tid, size):
            asked.append(size)
            return real(q, tid, size)

        wiki_search.ranked_ids = spy
        try:
            _search(client, who["student"], limit=5)
        finally:
            wiki_search.ranked_ids = real
        assert asked == [20]  # asked for more than it will show, because some may be hidden


class TestReindex:
    def test_an_admin_indexes_the_tenants_pages_and_only_those(self, client, db_session, who, pages, backend):
        backend.docs.clear()
        with acting_as(who["foreign"]):
            client.post("/wiki/spaces", json={"name": "F", "slug": "f", "visibility": "all"})
            client.post("/wiki/spaces/f/pages", json={"title": "Theirs", "body": "x"})
        backend.docs.clear()
        with acting_as(who["admin"]):
            r = client.post("/wiki/search/reindex")
        assert r.status_code == 200 and r.json()["indexed"] == 4
        assert {d["tenant_id"] for d in backend.docs.values()} == {DEV_TENANT}

    def test_an_editor_who_is_not_an_admin_cannot(self, client, who, pages, backend):
        with acting_as(who["ops"]):
            assert client.post("/wiki/search/reindex").status_code == 403
        with acting_as(who["student"]):
            assert client.post("/wiki/search/reindex").status_code == 403

    def test_it_says_so_when_indexing_is_off_or_the_store_is_down(self, client, who, pages, backend, monkeypatch):
        backend.fail = True
        with acting_as(who["admin"]):
            assert client.post("/wiki/search/reindex").status_code == 502
        monkeypatch.setenv("WIKI_OPENSEARCH", "false")
        with acting_as(who["admin"]):
            assert client.post("/wiki/search/reindex").status_code == 409


class TestOpenSearchRequests:
    """The wire shape of the new backend calls, against a fake server (no cluster runs in CI)."""

    @pytest.fixture
    def server(self, monkeypatch):
        seen: list[httpx.Request] = []
        state = {"exists": False, "bulk_status": 200}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "HEAD":
                return httpx.Response(200 if state["exists"] else 404)
            if request.method == "PUT":
                state["exists"] = True
                return httpx.Response(200, json={"acknowledged": True})
            if request.url.path == "/_bulk":
                rows = [json.loads(line) for line in request.content.decode().splitlines() if line]
                actions = [r for r in rows if len(r) == 1 and next(iter(r)) in ("index", "delete")]
                items = [{next(iter(a)): {"status": state["bulk_status"]}} for a in actions]
                return httpx.Response(200, json={"errors": False, "items": items})
            return httpx.Response(404)

        real = httpx.AsyncClient
        monkeypatch.setattr(
            opensearch.httpx, "AsyncClient", lambda **kw: real(**{**kw, "transport": httpx.MockTransport(handler)})
        )
        monkeypatch.setattr(opensearch, "_ensured", set())
        return seen, state

    def _be(self):
        return opensearch.OpenSearchBackend(url="http://os:9200")

    def test_upsert_creates_the_index_once_then_bulk_indexes_by_id(self, server):
        import asyncio

        seen, _ = server
        be = self._be()
        n = asyncio.run(
            be.upsert_documents("wiki", {"a": {"title": "x"}, "b": {"title": "y"}}, settings={"mappings": {}})
        )
        asyncio.run(be.upsert_documents("wiki", {"c": {"title": "z"}}, settings={"mappings": {}}))
        assert n == 2
        assert [r.method for r in seen].count("PUT") == 1
        bulk = [r for r in seen if r.url.path == "/_bulk"][0]
        first = json.loads(bulk.content.decode().splitlines()[0])
        assert first == {"index": {"_index": "wiki", "_id": "a"}}

    def test_delete_uses_bulk_delete_and_a_missing_document_is_fine(self, server):
        import asyncio

        _, state = server
        state["bulk_status"] = 404
        assert asyncio.run(self._be().delete_documents("wiki", ["a"])) == 1

    def test_a_404_on_an_index_operation_is_a_failure_not_success(self, server):
        import asyncio

        _, state = server
        state["bulk_status"] = 404
        with pytest.raises(SearchBackendError):
            asyncio.run(self._be().upsert_documents("wiki", {"a": {}}))

    def test_a_delete_as_the_first_write_still_creates_the_index_with_the_mapping(self, server):
        import asyncio

        seen, _ = server
        asyncio.run(self._be().delete_documents("wiki", ["a"], settings={"mappings": {"properties": {}}}))
        assert [r.method for r in seen][:2] == ["HEAD", "PUT"]

    def test_versions_are_sent_as_external_versions_and_a_conflict_is_not_an_error(self, server):
        import asyncio
        import json

        seen, state = server
        state["bulk_status"] = 409  # "a newer version is stored"
        n = asyncio.run(self._be().upsert_documents("wiki", {"a": {"t": 1}}, versions={"a": 123}))
        assert n == 1
        bulk = [r for r in seen if r.url.path == "/_bulk"][0]
        assert json.loads(bulk.content.decode().splitlines()[0]) == {
            "index": {"_index": "wiki", "_id": "a", "version": 123, "version_type": "external"}
        }

    def test_a_failure_makes_the_next_write_check_the_index_again(self, server, monkeypatch):
        import asyncio

        _, state = server
        be = self._be()
        asyncio.run(be.upsert_documents("wiki", {"a": {}}, settings={"mappings": {}}))
        assert "wiki" in opensearch._ensured

        def boom(**kw):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(opensearch.httpx, "AsyncClient", boom)
        with pytest.raises(SearchBackendError):
            asyncio.run(be.upsert_documents("wiki", {"a": {}}, settings={"mappings": {}}))
        assert "wiki" not in opensearch._ensured

    def test_a_rejected_document_or_an_unreachable_store_is_a_backend_error(self, server, monkeypatch):
        import asyncio

        _, state = server
        state["bulk_status"] = 400
        with pytest.raises(SearchBackendError):
            asyncio.run(self._be().upsert_documents("wiki", {"a": {}}))

        def boom(**kw):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(opensearch.httpx, "AsyncClient", boom)
        with pytest.raises(SearchBackendError):
            asyncio.run(self._be().delete_documents("wiki", ["a"]))
