"""Course releases: upload verification, acceptance, supersession, pinning, part separation,
immutability, roles and tenancy. Tarballs come from tools/arc2/release.py (see _release_kit)."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import uuid
from contextlib import contextmanager

import pytest
from _release_kit import CATALOGUE, CROSSWALK, build
from app.auth import CurrentUser, get_current_user
from app.course_releases.models import CourseRelease, ImmutableReleaseError
from app.course_releases.service import pinned_release
from app.enrollment import ensure_enrollment
from app.main import app as fastapi_app
from app.models import Course, CourseModule, User, UserRole

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


@contextmanager
def acting_as(role: UserRole, tenant: str = DEV_TENANT):
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


@pytest.fixture
def catalogue(client):
    client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", CROSSWALK.read_bytes(), "text/csv")})
    resp = client.post("/courses/import-programme", files={"file": ("c.csv", CATALOGUE.read_bytes(), "text/csv")})
    assert resp.status_code == 200, resp.text
    return client


def upload(client, data: bytes):
    return client.post("/course-releases", files={"file": ("release.tar.gz", data, "application/gzip")})


def retar(data: bytes, edit) -> bytes:
    """Rewrite a release tarball member by member; ``edit(name, bytes)`` returns new bytes,
    None to drop, or a list of extra (name, bytes) to add after release.json."""
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as src,
        gzip.GzipFile(fileobj=out, mode="wb") as gz,
        tarfile.open(fileobj=gz, mode="w") as dst,
    ):
        for info in src:
            content = src.extractfile(info).read()
            new = edit(info.name, content)
            extras = []
            if isinstance(new, list):
                extras, new = new, content
            if new is None:
                continue
            info.size = len(new)
            dst.addfile(info, io.BytesIO(new))
            for name, blob in extras:
                ti = tarfile.TarInfo(name)
                ti.size = len(blob)
                dst.addfile(ti, io.BytesIO(blob))
    return out.getvalue()


def _course(db) -> Course:
    for c in db.query(Course).all():
        if json.loads(c.course_meta or "{}").get("course_code") == "C304":
            return c
    raise AssertionError("C304 not imported")


def _student(db) -> User:
    u = User(
        email=f"s-{uuid.uuid4().hex[:6]}@example.test",
        display_name="s",
        role=UserRole.student,
        tenant_id=uuid.UUID(DEV_TENANT),
        keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
    )
    db.add(u)
    db.flush()
    return u


class TestUpload:
    def test_a_theory_release_becomes_a_candidate_once(self, catalogue, tmp_path):
        data = build(tmp_path)
        first = upload(catalogue, data)
        assert first.status_code == 201, first.text
        body = first.json()
        assert body["state"] == "candidate" and body["version"] == 1 and body["catalogue_code"] == "C304"
        assert set(body["activities"].values()) == {"theory"}
        again = upload(catalogue, data)
        assert again.status_code == 200 and again.json()["id"] == body["id"]

    def test_a_changed_file_is_refused(self, catalogue, tmp_path):
        data = retar(build(tmp_path), lambda n, b: b + b"tampered" if n.endswith("page-01.html") else b)
        resp = upload(catalogue, data)
        assert resp.status_code == 422 and "do not match release.json" in resp.json()["detail"]

    def test_instructor_material_in_the_learner_part_is_refused(self, catalogue, tmp_path):
        def smuggle(name, content):
            return (
                [("learner/02-content/mod_001/content/answer_key.html", b"Q1: B")]
                if name == "release.json"
                else content
            )

        resp = upload(catalogue, retar(build(tmp_path), smuggle))
        assert resp.status_code == 422 and "instructor material in the learner part" in resp.json()["detail"]

    def test_an_unknown_catalogue_course_is_refused(self, catalogue, tmp_path):
        resp = upload(catalogue, build(tmp_path, catalogue_code="C999"))
        assert resp.status_code == 422 and "no catalogue course 'C999'" in resp.json()["detail"]

    def test_a_range_release_needs_a_valid_lab_profile(self, catalogue, tmp_path):
        resp = upload(catalogue, build(tmp_path, range_ordinals=frozenset({1}), lab_profile=False))
        assert resp.status_code == 422 and "need a lab profile" in resp.json()["detail"]
        bad = dict(build.__globals__["LAB_PROFILE"], nodes=[])
        resp = upload(catalogue, build(tmp_path / "b", range_ordinals=frozenset({1}), lab_profile=bad))
        assert resp.status_code == 422 and "lab.schema" in resp.json()["detail"]

    def test_a_range_release_with_its_profile_is_accepted_as_candidate(self, catalogue, tmp_path):
        resp = upload(catalogue, build(tmp_path, range_ordinals=frozenset({1})))
        assert resp.status_code == 201, resp.text
        assert resp.json()["activities"]["mod_001"] == "range"

    def test_garbage_is_refused(self, catalogue):
        resp = upload(catalogue, b"not a tarball")
        assert resp.status_code == 422 and "not a gzipped tar" in resp.json()["detail"]


class TestAccept:
    def test_open_actions_must_be_acknowledged(self, catalogue, tmp_path, db_session):
        actions = [{"id": "po.candidate_needs_standards:x", "category": "standards", "text": "Standards decides"}]
        rid = upload(catalogue, build(tmp_path, open_actions=actions)).json()["id"]
        refused = catalogue.post(f"/course-releases/{rid}/accept", json={})
        assert refused.status_code == 409 and "po.candidate_needs_standards:x" in refused.json()["detail"]
        ok = catalogue.post(
            f"/course-releases/{rid}/accept", json={"acknowledge_actions": ["po.candidate_needs_standards:x"]}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["state"] == "accepted"
        assert ok.json()["acknowledged_actions"] == ["po.candidate_needs_standards:x"]

    def test_accept_imports_the_content_onto_the_catalogue_course(self, catalogue, tmp_path, db_session):
        rid = upload(catalogue, build(tmp_path)).json()["id"]
        status_before = catalogue.get(f"/course-releases/courses/{_course(db_session).id}").json()
        assert status_before["legacy"] is True and status_before["candidates"] == 1
        assert catalogue.post(f"/course-releases/{rid}/accept", json={}).status_code == 200
        course = _course(db_session)
        modules = db_session.query(CourseModule).filter_by(course_id=course.id).all()
        assert len(modules) == 6
        assert {json.loads(m.content_ref)["activity"] for m in modules} == {"theory"}
        assert course.is_published is False
        status_after = catalogue.get(f"/course-releases/courses/{course.id}").json()
        assert status_after["legacy"] is False and status_after["active_version"] == 1
        assert catalogue.post(f"/course-releases/{rid}/accept", json={}).status_code == 409

    def test_a_new_release_supersedes_and_pins_do_not_move(self, catalogue, tmp_path, db_session):
        first = upload(catalogue, build(tmp_path / "a")).json()["id"]
        catalogue.post(f"/course-releases/{first}/accept", json={})
        course = _course(db_session)
        early = ensure_enrollment(
            db_session, user_id=_student(db_session).id, course_id=course.id, tenant_id=DEV_TENANT
        )
        assert str(pinned_release(db_session, early.id).id) == first

        second = upload(catalogue, build(tmp_path / "b", title_suffix=" (rev)", slug="arc2-iot-b")).json()
        assert second["version"] == 2
        assert catalogue.post(f"/course-releases/{second['id']}/accept", json={}).status_code == 200
        assert catalogue.get(f"/course-releases/{first}").json()["state"] == "superseded"
        late = ensure_enrollment(db_session, user_id=_student(db_session).id, course_id=course.id, tenant_id=DEV_TENANT)
        assert str(pinned_release(db_session, late.id).id) == second["id"]
        assert str(pinned_release(db_session, early.id).id) == first

    def test_a_course_without_a_release_leaves_enrollments_unpinned(self, catalogue, db_session):
        course = _course(db_session)
        e = ensure_enrollment(db_session, user_id=_student(db_session).id, course_id=course.id, tenant_id=DEV_TENANT)
        assert pinned_release(db_session, e.id) is None


class TestParts:
    def test_the_learner_bundle_carries_no_instructor_or_platform_files(self, catalogue, tmp_path):
        rid = upload(catalogue, build(tmp_path)).json()["id"]
        resp = catalogue.get(f"/course-releases/{rid}/learner-bundle")
        assert resp.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            names = tar.getnames()
        assert names and all(n.startswith("02-content/mod_") and "/content/" in n for n in names)

    def test_the_instructor_bundle_is_the_instructor_pack(self, catalogue, tmp_path):
        rid = upload(catalogue, build(tmp_path)).json()["id"]
        resp = catalogue.get(f"/course-releases/{rid}/instructor-bundle")
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            assert set(tar.getnames()) == {"04-artifacts/rubric.md", "04-artifacts/instructor/answer_key.md"}


class TestAccess:
    def test_students_get_neither_the_pack_nor_acceptance(self, catalogue, tmp_path):
        rid = upload(catalogue, build(tmp_path)).json()["id"]
        with acting_as(UserRole.student):
            assert catalogue.get(f"/course-releases/{rid}/instructor-bundle").status_code == 403
            assert catalogue.post(f"/course-releases/{rid}/accept", json={}).status_code == 403
            assert upload(catalogue, build(tmp_path / "s", slug="arc2-iot-s")).status_code == 403

    def test_instructors_can_author_and_accept(self, catalogue, tmp_path):
        with acting_as(UserRole.instructor):
            rid = upload(catalogue, build(tmp_path)).json()["id"]
            assert catalogue.post(f"/course-releases/{rid}/accept", json={}).status_code == 200

    def test_another_tenant_sees_nothing(self, catalogue, tmp_path):
        rid = upload(catalogue, build(tmp_path)).json()["id"]
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            assert catalogue.get(f"/course-releases/{rid}").status_code == 404
            assert catalogue.get(f"/course-releases/{rid}/instructor-bundle").status_code == 404
            assert catalogue.post(f"/course-releases/{rid}/accept", json={}).status_code == 404
            assert catalogue.get("/course-releases").json() == []


class TestImmutability:
    def test_content_columns_cannot_change(self, catalogue, tmp_path, db_session):
        rid = upload(catalogue, build(tmp_path)).json()["id"]
        release = db_session.get(CourseRelease, uuid.UUID(rid))
        release.release_digest = "f" * 64
        with pytest.raises(ImmutableReleaseError, match="release_digest is fixed"):
            db_session.flush()
        db_session.rollback()

    def test_state_only_moves_forward(self, catalogue, tmp_path, db_session):
        rid = upload(catalogue, build(tmp_path)).json()["id"]
        catalogue.post(f"/course-releases/{rid}/accept", json={})
        release = db_session.get(CourseRelease, uuid.UUID(rid))
        release.state = "candidate"
        with pytest.raises(ImmutableReleaseError, match="accepted → candidate"):
            db_session.flush()
        db_session.rollback()
