"""Images in wiki pages (#379): who can upload, who can see, and that nothing hostile gets through.

The negatives matter most: another tenant, a staff-only space, an unpublished page, a deleted
page, an archived space — each a 404 for whoever may not see the page, because the page's
visibility is the image's. Knowing the id grants nothing.
"""

import uuid
from contextlib import contextmanager

import pytest
from app import object_store
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import Tenant, User, UserRole
from app.models_wiki import WikiImage, WikiSpace

from tests.api import wiki_image_bytes as img

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


@pytest.fixture
def store(monkeypatch):
    objs: dict[str, bytes] = {}
    monkeypatch.setattr(object_store, "put_object", lambda k, d, c, bucket=None: objs.__setitem__(k, d))
    monkeypatch.setattr(object_store, "get_object", lambda k, bucket=None: objs[k])
    monkeypatch.setattr(object_store, "delete_object", lambda k, bucket=None: objs.pop(k, None))
    return objs


@pytest.fixture
def who(db_session):
    return {
        "admin": _person(db_session, UserRole.admin),
        "ops": _person(db_session, UserRole.range_ops),
        "student": _person(db_session, UserRole.student),
        "foreign": _person(db_session, UserRole.range_ops, OTHER_TENANT),
    }


@pytest.fixture
def page(client, who):
    """A published page in a space every role can read."""
    with acting_as(who["admin"]):
        assert (
            client.post("/wiki/spaces", json={"name": "Help", "slug": "help", "visibility": "all"}).status_code == 201
        )
    with acting_as(who["ops"]):
        r = client.post("/wiki/spaces/help/pages", json={"title": "Start", "body": "hi"})
        assert r.status_code == 201, r.text
        return r.json()


def _up(client, page_id, data, name="shot.png", ctype="image/png"):
    return client.post(f"/wiki/pages/{page_id}/images", files={"file": (name, data, ctype)})


class TestUpload:
    def test_an_editor_uploads_a_png_and_gets_the_markdown_path(self, client, who, page, store):
        with acting_as(who["ops"]):
            r = _up(client, page["id"], img.png(5, 4))
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["url"] == f"/api/wiki/images/{body['id']}"
        assert (body["width"], body["height"], body["content_type"]) == (5, 4, "image/png")
        assert len(store) == 1

    def test_a_student_cannot_upload(self, client, who, page, store):
        with acting_as(who["student"]):
            assert _up(client, page["id"], img.png()).status_code == 403
        assert store == {}

    def test_the_type_comes_from_the_bytes_not_the_filename_or_header(self, client, who, page, store):
        with acting_as(who["ops"]):
            r = _up(client, page["id"], img.jpeg(), name="evil.svg", ctype="image/svg+xml")
        assert r.status_code == 201 and r.json()["content_type"] == "image/jpeg"

    @pytest.mark.parametrize("data", [img.SVG, b"<html><script>alert(1)</script></html>", b"MZ\x90\x00", b""])
    def test_svg_html_executables_and_empty_files_are_refused_even_named_png(self, client, who, page, store, data):
        with acting_as(who["ops"]):
            r = _up(client, page["id"], data, name="x.png", ctype="image/png")
        assert r.status_code == 422
        assert store == {}

    def test_an_oversized_file_is_refused_without_being_stored(self, client, who, page, store):
        with acting_as(who["ops"]):
            r = _up(client, page["id"], img.png() + b"\x00" * (5 * 1024 * 1024))
        assert r.status_code == 422 and store == {}

    def test_metadata_is_gone_from_what_is_stored(self, client, who, page, store):
        with acting_as(who["ops"]):
            assert _up(client, page["id"], img.jpeg(), name="photo.jpg", ctype="image/jpeg").status_code == 201
        (stored,) = store.values()
        assert img.GPS not in stored

    def test_the_filename_is_cleaned(self, client, who, page, store):
        with acting_as(who["ops"]):
            r = _up(client, page["id"], img.png(), name="../../etc/<script>x.png")
        assert r.json()["filename"] == "x.png" or "/" not in r.json()["filename"]
        assert "<" not in r.json()["filename"] and ".." not in r.json()["filename"].replace("x.png", "")

    def test_another_tenants_page_is_a_404(self, client, who, page, store):
        with acting_as(who["foreign"]):
            assert _up(client, page["id"], img.png()).status_code == 404

    def test_a_missing_page_is_a_404(self, client, who, store):
        with acting_as(who["ops"]):
            assert _up(client, str(uuid.uuid4()), img.png()).status_code == 404

    def test_an_archived_space_is_read_only(self, client, db_session, who, page, store):
        db_session.query(WikiSpace).update({"is_archived": True})
        db_session.commit()
        with acting_as(who["ops"]):
            assert _up(client, page["id"], img.png()).status_code == 409

    def test_generated_pages_cannot_hold_uploads(self, client, db_session, who, page, store):
        db_session.query(WikiSpace).update({"system_owned": True})
        db_session.commit()
        with acting_as(who["ops"]):
            assert _up(client, page["id"], img.png()).status_code == 409

    def test_a_page_holds_a_bounded_number(self, client, monkeypatch, who, page, store):
        from app.routers import wiki_images

        monkeypatch.setattr(wiki_images, "MAX_IMAGES_PER_PAGE", 2)
        with acting_as(who["ops"]):
            assert _up(client, page["id"], img.png()).status_code == 201
            assert _up(client, page["id"], img.png()).status_code == 201
            assert _up(client, page["id"], img.png()).status_code == 409

    def test_a_storage_failure_leaves_no_row(self, client, db_session, monkeypatch, who, page, store):
        def boom(*a, **k):
            raise RuntimeError("minio down")

        monkeypatch.setattr(object_store, "put_object", boom)
        with acting_as(who["ops"]), pytest.raises(RuntimeError):
            _up(client, page["id"], img.png())
        assert db_session.query(WikiImage).count() == 0


class TestRead:
    @pytest.fixture
    def uploaded(self, client, who, page, store):
        with acting_as(who["ops"]):
            return _up(client, page["id"], img.png(3, 3)).json()

    def test_a_student_reads_an_image_on_a_published_page(self, client, who, uploaded):
        with acting_as(who["student"]):
            r = client.get(f"/wiki/images/{uploaded['id']}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content.startswith(b"\x89PNG")

    def test_it_is_served_so_it_cannot_run(self, client, who, uploaded):
        with acting_as(who["student"]):
            h = client.get(f"/wiki/images/{uploaded['id']}").headers
        assert h["x-content-type-options"] == "nosniff"
        assert "sandbox" in h["content-security-policy"] and "default-src 'none'" in h["content-security-policy"]
        assert h["cache-control"].startswith("private")

    def test_another_tenant_cannot_read_it(self, client, who, uploaded):
        with acting_as(who["foreign"]):
            assert client.get(f"/wiki/images/{uploaded['id']}").status_code == 404

    def test_an_unpublished_page_hides_its_images_from_students_only(self, client, who, page, uploaded):
        with acting_as(who["ops"]):
            r = client.put(
                f"/wiki/pages/{page['id']}",
                json={"base_revision": page["revision_number"], "is_published": False},
            )
            assert r.status_code == 200, r.text
            assert client.get(f"/wiki/images/{uploaded['id']}").status_code == 200
        with acting_as(who["student"]):
            assert client.get(f"/wiki/images/{uploaded['id']}").status_code == 404

    def test_a_staff_only_space_hides_its_images_from_students(self, client, db_session, who, uploaded):
        db_session.query(WikiSpace).update({"visibility": "staff"})
        db_session.commit()
        with acting_as(who["student"]):
            assert client.get(f"/wiki/images/{uploaded['id']}").status_code == 404

    def test_a_deleted_page_hides_its_images(self, client, who, page, uploaded):
        with acting_as(who["ops"]):
            assert client.delete(f"/wiki/pages/{page['id']}").status_code == 204
            assert client.get(f"/wiki/images/{uploaded['id']}").status_code == 404

    def test_an_image_in_a_hidden_page_stays_hidden_when_another_page_embeds_it(self, client, who, page, uploaded):
        with acting_as(who["ops"]):
            hidden = client.post(
                "/wiki/spaces/help/pages", json={"title": "Draft", "body": "", "is_published": False}
            ).json()
            up = _up(client, hidden["id"], img.png()).json()
            client.put(
                f"/wiki/pages/{page['id']}",
                json={"base_revision": page["revision_number"], "body": f"![x]({up['url']})"},
            )
        with acting_as(who["student"]):
            assert client.get(up["url"].removeprefix("/api")).status_code == 404

    def test_a_malformed_or_unknown_id_is_a_404_or_422_never_a_500(self, client, who, store):
        with acting_as(who["student"]):
            assert client.get(f"/wiki/images/{uuid.uuid4()}").status_code == 404
            assert client.get("/wiki/images/not-a-uuid").status_code == 422

    def test_storage_down_is_a_502(self, client, monkeypatch, who, uploaded):
        def boom(*a, **k):
            raise RuntimeError("minio down")

        monkeypatch.setattr(object_store, "get_object", boom)
        with acting_as(who["student"]):
            assert client.get(f"/wiki/images/{uploaded['id']}").status_code == 502

    def test_a_row_whose_type_is_not_an_image_type_is_never_served(self, client, db_session, who, uploaded):
        db_session.query(WikiImage).update({"content_type": "text/html"})
        db_session.commit()
        with acting_as(who["student"]):
            assert client.get(f"/wiki/images/{uploaded['id']}").status_code == 404


class TestList:
    def test_an_editor_sees_the_pages_images_and_a_student_does_not(self, client, who, page, store):
        with acting_as(who["ops"]):
            a = _up(client, page["id"], img.png()).json()
            b = _up(client, page["id"], img.gif()).json()
            r = client.get(f"/wiki/pages/{page['id']}/images")
            assert {i["id"] for i in r.json()} == {a["id"], b["id"]}
            assert client.delete(f"/wiki/images/{a['id']}").status_code == 204
            assert [i["id"] for i in client.get(f"/wiki/pages/{page['id']}/images").json()] == [b["id"]]
        with acting_as(who["student"]):
            assert client.get(f"/wiki/pages/{page['id']}/images").status_code == 403

    def test_another_tenant_gets_a_404(self, client, who, page, store):
        with acting_as(who["foreign"]):
            assert client.get(f"/wiki/pages/{page['id']}/images").status_code == 404


class TestCaching:
    def test_the_cache_is_short_private_and_varies_by_credentials(self, client, who, page, store):
        with acting_as(who["ops"]):
            up = _up(client, page["id"], img.png()).json()
            h = client.get(f"/wiki/images/{up['id']}").headers
        assert h["cache-control"] == "private, max-age=300" and "Authorization" in h["vary"]

    def test_a_failed_commit_removes_the_stored_object(self, client, db_session, monkeypatch, who, page, store):
        from sqlalchemy.orm import Session

        real = Session.commit

        def boom(self):
            raise RuntimeError("db down")

        monkeypatch.setattr(Session, "commit", boom)
        with acting_as(who["ops"]), pytest.raises(RuntimeError):
            _up(client, page["id"], img.png())
        monkeypatch.setattr(Session, "commit", real)
        assert store == {}


class TestDelete:
    def test_an_editor_removes_it_and_it_is_gone(self, client, who, page, store):
        with acting_as(who["ops"]):
            up = _up(client, page["id"], img.png()).json()
            assert client.delete(f"/wiki/images/{up['id']}").status_code == 204
            assert client.get(f"/wiki/images/{up['id']}").status_code == 404
        assert store == {}

    def test_a_student_cannot_remove_it(self, client, who, page, store):
        with acting_as(who["ops"]):
            up = _up(client, page["id"], img.png()).json()
        with acting_as(who["student"]):
            assert client.delete(f"/wiki/images/{up['id']}").status_code == 403

    def test_another_tenant_cannot_remove_it(self, client, who, page, store):
        with acting_as(who["ops"]):
            up = _up(client, page["id"], img.png()).json()
        with acting_as(who["foreign"]):
            assert client.delete(f"/wiki/images/{up['id']}").status_code == 404
        assert len(store) == 1
