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


# -- review findings (2026-10-05) ---------------------------------------------


def _meta_and_members(data: bytes) -> tuple[dict, dict[str, bytes]]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar}
    return json.loads(members["release.json"]), members


def _repack(meta: dict, members: dict[str, bytes], *, redigest: bool) -> bytes:
    from arc2 import release as arc_release

    if redigest:
        meta["release_digest"] = arc_release.release_digest(meta)
    members = dict(members, **{"release.json": json.dumps(meta).encode()})
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb") as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for name, blob in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            tar.addfile(info, io.BytesIO(blob))
    return out.getvalue()


class TestReleaseJsonIsVerified:
    def test_dropping_open_actions_breaks_the_digest(self, catalogue, tmp_path):
        actions = [{"id": "po.x", "category": "standards", "text": "Standards decides"}]
        meta, members = _meta_and_members(build(tmp_path, open_actions=actions))
        meta["open_human_actions"] = []
        resp = upload(catalogue, _repack(meta, members, redigest=False))
        assert resp.status_code == 422 and "release_digest does not match" in resp.json()["detail"]

    def test_a_redigested_release_json_must_still_match_the_run(self, catalogue, tmp_path):
        actions = [{"id": "po.x", "category": "standards", "text": "Standards decides"}]
        meta, members = _meta_and_members(build(tmp_path, open_actions=actions))
        meta["open_human_actions"] = []
        resp = upload(catalogue, _repack(meta, members, redigest=True))
        assert resp.status_code == 422 and "open actions differ from the run's" in resp.json()["detail"]

    def test_a_range_module_cannot_be_left_out_of_the_activities(self, catalogue, tmp_path):
        meta, members = _meta_and_members(build(tmp_path, range_ordinals=frozenset({2})))
        del meta["activities"]["mod_002"]
        resp = upload(catalogue, _repack(meta, members, redigest=True))
        assert resp.status_code == 422 and "activities differ from the run's modules" in resp.json()["detail"]

    def test_a_run_without_passed_qa_is_refused(self, catalogue, tmp_path):
        meta, members = _meta_and_members(build(tmp_path))
        manifest = json.loads(members["platform/manifest.json"])
        manifest["qa"]["result"] = "fail"
        blob = json.dumps(manifest).encode()
        members["platform/manifest.json"] = blob
        from arc2 import release as arc_release

        entry = next(f for f in meta["parts"]["platform"]["files"] if f["path"] == "manifest.json")
        entry["sha256"] = __import__("hashlib").sha256(blob).hexdigest()
        meta["parts"]["platform"]["digest"] = arc_release.digest_files(meta["parts"]["platform"]["files"])
        resp = upload(catalogue, _repack(meta, members, redigest=True))
        assert resp.status_code == 422 and "QA did not pass" in resp.json()["detail"]

    def test_an_expansion_bomb_is_refused_before_tar_reads_it(self, catalogue, monkeypatch):
        from app.course_releases import bundle

        monkeypatch.setattr(bundle, "MAX_EXPANDED", 1024)
        bomb = gzip.compress(b"\0" * 4096)
        resp = upload(catalogue, bomb)
        assert resp.status_code == 422 and "expands beyond 1024 bytes" in resp.json()["detail"]


class TestAcceptOrdering:
    def test_an_older_candidate_cannot_replace_a_newer_release(self, catalogue, tmp_path):
        v1 = upload(catalogue, build(tmp_path / "a")).json()["id"]
        v2 = upload(catalogue, build(tmp_path / "b", title_suffix=" (rev)", slug="arc2-iot-b")).json()["id"]
        assert catalogue.post(f"/course-releases/{v2}/accept", json={}).status_code == 200
        resp = catalogue.post(f"/course-releases/{v1}/accept", json={})
        assert resp.status_code == 409 and "is older and would replace newer content" in resp.json()["detail"]

    def test_releases_are_never_deleted(self, catalogue, tmp_path, db_session):
        rid = upload(catalogue, build(tmp_path)).json()["id"]
        db_session.delete(db_session.get(CourseRelease, uuid.UUID(rid)))
        with pytest.raises(ImmutableReleaseError, match="never deleted"):
            db_session.flush()
        db_session.rollback()

    def test_the_acceptance_record_is_written_once(self, catalogue, tmp_path, db_session):
        rid = upload(catalogue, build(tmp_path)).json()["id"]
        catalogue.post(f"/course-releases/{rid}/accept", json={"notes": "first"})
        release = db_session.get(CourseRelease, uuid.UUID(rid))
        release.notes = "rewritten"
        with pytest.raises(ImmutableReleaseError, match="written once"):
            db_session.flush()
        db_session.rollback()


class TestAcceptKeepsStudentsWork:
    def _attempt(self, db, course, user):
        from app.course_content_ingest import _current_quiz
        from app.models import QuizAttempt

        module = db.query(CourseModule).filter_by(course_id=course.id, ordinal=1).one()
        quiz = _current_quiz(db, module)
        db.add(QuizAttempt(quiz_id=quiz.id, user_id=user.id))
        db.flush()
        return quiz

    def test_a_changed_quiz_with_attempts_is_retired_not_rewritten(self, catalogue, tmp_path, db_session):
        from app.course_content_ingest import _current_quiz

        v1 = upload(catalogue, build(tmp_path / "a")).json()["id"]
        catalogue.post(f"/course-releases/{v1}/accept", json={})
        course = _course(db_session)
        course.is_published = True
        db_session.commit()
        old_quiz = self._attempt(db_session, course, _student(db_session))
        old_ids = sorted(str(q.id) for q in old_quiz.questions)

        v2 = upload(catalogue, _with_revised_question(tmp_path / "b")).json()["id"]
        assert catalogue.post(f"/course-releases/{v2}/accept", json={}).status_code == 200
        db_session.expire_all()
        module = db_session.query(CourseModule).filter_by(course_id=course.id, ordinal=1).one()
        new_quiz = _current_quiz(db_session, module)
        assert new_quiz.id != old_quiz.id
        assert sorted(str(q.id) for q in db_session.get(type(old_quiz), old_quiz.id).questions) == old_ids
        assert _course(db_session).is_published is True  # accepting a release does not unpublish

    def test_an_unchanged_quiz_keeps_its_questions(self, catalogue, tmp_path, db_session):
        from app.course_content_ingest import _current_quiz

        v1 = upload(catalogue, build(tmp_path / "a")).json()["id"]
        catalogue.post(f"/course-releases/{v1}/accept", json={})
        course = _course(db_session)
        quiz = self._attempt(db_session, course, _student(db_session))
        ids = sorted(str(q.id) for q in quiz.questions)
        v2 = upload(catalogue, build(tmp_path / "b", title_suffix=" (rev)", slug="arc2-iot-b")).json()["id"]
        catalogue.post(f"/course-releases/{v2}/accept", json={})
        module = db_session.query(CourseModule).filter_by(course_id=course.id, ordinal=1).one()
        assert _current_quiz(db_session, module).id == quiz.id
        assert sorted(str(q.id) for q in _current_quiz(db_session, module).questions) == ids

    def test_dropped_modules_retire_and_new_ones_get_progress(self, catalogue, tmp_path, db_session):
        from app.models import ModuleProgress

        v1 = upload(catalogue, build(tmp_path / "a", drop_ordinals=frozenset({6}))).json()["id"]
        catalogue.post(f"/course-releases/{v1}/accept", json={})
        course = _course(db_session)
        e = ensure_enrollment(db_session, user_id=_student(db_session).id, course_id=course.id, tenant_id=DEV_TENANT)
        assert db_session.query(ModuleProgress).filter_by(enrollment_id=e.id).count() == 5
        v2 = upload(catalogue, build(tmp_path / "b", drop_ordinals=frozenset({1}), slug="arc2-iot-b")).json()["id"]
        assert catalogue.post(f"/course-releases/{v2}/accept", json={}).status_code == 200
        first = db_session.query(CourseModule).filter_by(course_id=course.id, ordinal=1).one()
        assert json.loads(first.content_ref)["retired"] is True and first.is_required is False
        assert db_session.query(ModuleProgress).filter_by(enrollment_id=e.id).count() == 6


def _with_revised_question(root):
    from _release_kit import write_run
    from arc2 import release as arc_release

    run = root / "arc2-iot-rev"
    write_run(run)
    course_file = run / "02-content/arc2-iot.yaml"
    import yaml as _yaml

    doc = _yaml.safe_load(course_file.read_text())
    doc["modules"][0]["quiz"]["questions"][0]["question"] += " (revised)"
    course_file.write_text(_yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    meta, _ = _meta_and_members(build(root / "base"))
    parts = arc_release.collect(run)
    meta["slug"] = "arc2-iot-rev"
    meta["parts"] = {n: {"digest": arc_release.digest_files(f), "files": f} for n, f in parts.items()}
    meta["release_digest"] = arc_release.release_digest(meta)
    return arc_release._tarball(run, meta)
