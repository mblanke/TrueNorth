"""Wiki: spaces, page tree, revisions, optimistic concurrency, visibility, search.

The negatives matter most: another tenant's page, a staff-only space, or an
unpublished page must be a 404 to whoever may not see it — never a 403, which
would confirm the thing exists.
"""

import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import UserRole
from app.models_wiki import WikiPage, WikiSpace

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


@contextmanager
def acting_as(role: UserRole, *, tenant: str = DEV_TENANT):
    who = CurrentUser(
        id=str(uuid.uuid4()),
        email=f"{role.value}@example.test",
        display_name=role.value,
        role=role,
        tenant_id=tenant,
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _space(client, slug="runbooks", visibility="all"):
    resp = client.post("/wiki/spaces", json={"name": slug.title(), "slug": slug, "visibility": visibility})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _page(client, space="runbooks", title="Home", body="hello", parent_id=None, **extra):
    resp = client.post(
        f"/wiki/spaces/{space}/pages", json={"title": title, "body": body, "parent_id": parent_id, **extra}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestSpacesAndPages:
    def test_create_space_and_page_round_trip(self, client):
        _space(client)
        page = _page(client, title="VM won't boot", body="# Steps\n1. Check power")
        assert page["slug"] == "vm-won-t-boot"
        assert page["revision_number"] == 1
        got = client.get(f"/wiki/pages/{page['id']}").json()
        assert got["body"] == "# Steps\n1. Check power"
        assert got["space_slug"] == "runbooks"

    def test_duplicate_space_slug_is_409(self, client):
        _space(client)
        assert client.post("/wiki/spaces", json={"name": "x", "slug": "runbooks"}).status_code == 409

    def test_tree_nests_children_and_breadcrumbs_follow(self, client):
        _space(client)
        root = _page(client, title="Root")
        child = _page(client, title="Child", parent_id=root["id"])
        grandchild = _page(client, title="Grandchild", parent_id=child["id"])

        tree = client.get("/wiki/spaces/runbooks/tree").json()
        assert [n["title"] for n in tree] == ["Root"]
        assert tree[0]["children"][0]["children"][0]["id"] == grandchild["id"]

        got = client.get(f"/wiki/pages/{grandchild['id']}").json()
        assert [c["title"] for c in got["breadcrumbs"]] == ["Root", "Child"]
        assert client.get(f"/wiki/pages/{root['id']}").json()["children"][0]["title"] == "Child"

    def test_archived_space_leaves_the_list(self, client):
        _space(client)
        assert client.delete("/wiki/spaces/runbooks").status_code == 204
        assert client.get("/wiki/spaces").json() == []
        assert len(client.get("/wiki/spaces?include_archived=true").json()) == 1

    def test_delete_page_takes_its_children_with_it(self, client):
        _space(client)
        root = _page(client, title="Root")
        _page(client, title="Child", parent_id=root["id"])
        assert client.delete(f"/wiki/pages/{root['id']}").status_code == 204
        assert client.get("/wiki/spaces/runbooks/tree").json() == []
        assert client.get(f"/wiki/pages/{root['id']}").status_code == 404


class TestRevisions:
    def test_each_content_save_is_a_revision(self, client):
        _space(client)
        page = _page(client, body="v1")
        r = client.put(f"/wiki/pages/{page['id']}", json={"base_revision": 1, "body": "v2", "edit_summary": "fix"})
        assert r.status_code == 200 and r.json()["revision_number"] == 2

        revs = client.get(f"/wiki/pages/{page['id']}/revisions").json()
        assert [r["revision_number"] for r in revs] == [2, 1]
        assert revs[0]["edit_summary"] == "fix"
        assert client.get(f"/wiki/pages/{page['id']}/revisions/1").json()["body"] == "v1"

    def test_stale_base_revision_is_409_with_the_current_page(self, client):
        _space(client)
        page = _page(client, body="v1")
        client.put(f"/wiki/pages/{page['id']}", json={"base_revision": 1, "body": "theirs"})
        clash = client.put(f"/wiki/pages/{page['id']}", json={"base_revision": 1, "body": "mine"})
        assert clash.status_code == 409
        assert clash.json()["current"]["body"] == "theirs"
        assert client.get(f"/wiki/pages/{page['id']}").json()["body"] == "theirs"

    def test_restore_creates_a_new_revision_and_keeps_history(self, client):
        _space(client)
        page = _page(client, body="original")
        client.put(f"/wiki/pages/{page['id']}", json={"base_revision": 1, "body": "vandalised"})
        restored = client.post(f"/wiki/pages/{page['id']}/revisions/1/restore", json={"base_revision": 2}).json()
        assert restored["body"] == "original"
        assert restored["revision_number"] == 3
        assert len(client.get(f"/wiki/pages/{page['id']}/revisions").json()) == 3

    def test_metadata_change_is_a_revision_with_a_summary(self, client):
        _space(client)
        page = _page(client)
        r = client.put(f"/wiki/pages/{page['id']}", json={"base_revision": 1, "tags": "vsphere", "is_published": False})
        assert r.json()["revision_number"] == 2
        assert client.get(f"/wiki/pages/{page['id']}/revisions").json()[0]["edit_summary"] == "Tags, unpublished"

    def test_saving_nothing_new_changes_nothing(self, client):
        _space(client)
        page = _page(client, body="same")
        r = client.put(f"/wiki/pages/{page['id']}", json={"base_revision": 1, "body": "same", "tags": ""})
        assert r.status_code == 200 and r.json()["revision_number"] == 1

    def test_unpublish_cannot_be_undone_by_a_stale_content_save(self, client):
        """B hides the page; A, still on the revision before that, fixes a typo -> 409, not re-published."""
        _space(client)
        page = _page(client, body="v1")
        hidden = client.put(f"/wiki/pages/{page['id']}", json={"base_revision": 1, "is_published": False})
        assert hidden.status_code == 200
        stale = client.put(
            f"/wiki/pages/{page['id']}", json={"base_revision": 1, "body": "v1 typo fixed", "is_published": True}
        )
        assert stale.status_code == 409
        assert client.get(f"/wiki/pages/{page['id']}").json()["is_published"] is False

    def test_page_cannot_move_under_its_own_descendant(self, client):
        _space(client)
        root = _page(client, title="Root")
        child = _page(client, title="Child", parent_id=root["id"])
        r = client.put(f"/wiki/pages/{root['id']}", json={"base_revision": 1, "parent_id": child["id"]})
        assert r.status_code == 422
        r = client.put(f"/wiki/pages/{root['id']}", json={"base_revision": 1, "parent_id": root["id"]})
        assert r.status_code == 422


class TestVisibility:
    @pytest.fixture
    def seeded(self, client):
        _space(client, "handbook")
        _space(client, "staff-only", visibility="staff")
        public = _page(client, "handbook", title="Welcome", body="range rules")
        draft = _page(client, "handbook", title="Draft", body="range secrets", is_published=False)
        hidden = _page(client, "staff-only", title="Answers", body="range answer key")
        return public, draft, hidden

    def test_student_sees_only_published_pages_in_open_spaces(self, client, seeded):
        public, draft, hidden = seeded
        with acting_as(UserRole.student):
            assert [s["slug"] for s in client.get("/wiki/spaces").json()] == ["handbook"]
            assert client.get("/wiki/spaces/staff-only").status_code == 404
            assert client.get(f"/wiki/pages/{public['id']}").status_code == 200
            assert client.get(f"/wiki/pages/{draft['id']}").status_code == 404
            assert client.get(f"/wiki/pages/{hidden['id']}").status_code == 404
            assert [n["title"] for n in client.get("/wiki/spaces/handbook/tree").json()] == ["Welcome"]
            assert [h["title"] for h in client.get("/wiki/search?q=range").json()] == ["Welcome"]

    def test_student_cannot_edit(self, client, seeded):
        public, _, _ = seeded
        with acting_as(UserRole.student):
            assert client.put(f"/wiki/pages/{public['id']}", json={"base_revision": 1, "body": "x"}).status_code == 403
            assert client.post("/wiki/spaces/handbook/pages", json={"title": "x"}).status_code == 403

    def test_instructor_edits_but_cannot_create_spaces(self, client, seeded):
        public, _, hidden = seeded
        with acting_as(UserRole.instructor):
            assert client.get(f"/wiki/pages/{hidden['id']}").status_code == 200
            assert client.put(f"/wiki/pages/{public['id']}", json={"base_revision": 1, "body": "y"}).status_code == 200
            assert client.post("/wiki/spaces", json={"name": "n", "slug": "n"}).status_code == 403

    def test_another_tenant_sees_nothing(self, client, seeded, db_session):
        public, _, _ = seeded
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            assert client.get("/wiki/spaces").json() == []
            assert client.get(f"/wiki/pages/{public['id']}").status_code == 404
            assert client.get("/wiki/spaces/handbook").status_code == 404
            assert client.get("/wiki/search?q=range").json() == []
            r = client.put(f"/wiki/pages/{public['id']}", json={"base_revision": 1, "body": "pwned"})
            assert r.status_code == 404
        assert db_session.query(WikiPage).filter_by(title="Welcome").one().body == "range rules"
        assert db_session.query(WikiSpace).count() == 2


class TestSearch:
    def test_title_matches_rank_first_and_wildcards_are_literal(self, client):
        _space(client)
        _page(client, title="Notes", body="how to reset vsphere host")
        _page(client, title="vSphere reset", body="see notes")
        hits = client.get("/wiki/search?q=vsphere").json()
        assert [h["title"] for h in hits] == ["vSphere reset", "Notes"]
        assert "vsphere" in hits[1]["snippet"]
        assert client.get("/wiki/search?q=%25%25").json() == []


class TestSecurityReviewFixes:
    def test_students_cannot_read_revision_history(self, client):
        """An unpublished draft (an answer key) must not be readable once the page is published."""
        _space(client)
        page = _page(client, body="ANSWER KEY: flag{x}", is_published=False)
        client.put(f"/wiki/pages/{page['id']}", json={"base_revision": 1, "body": "Student instructions", "is_published": True})
        with acting_as(UserRole.student):
            assert client.get(f"/wiki/pages/{page['id']}").json()["body"] == "Student instructions"
            assert client.get(f"/wiki/pages/{page['id']}/revisions").status_code == 403
            assert client.get(f"/wiki/pages/{page['id']}/revisions/1").status_code == 403

    def test_archived_space_takes_no_new_pages(self, client):
        _space(client)
        client.delete("/wiki/spaces/runbooks")
        assert client.post("/wiki/spaces/runbooks/pages", json={"title": "x"}).status_code == 409
