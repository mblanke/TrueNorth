# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Round-2 coverage for app/cmi5/router.py: the error paths of the cmi5 HTTP surface.

``test_cmi5_lms.py`` covers the launch/LRS happy path; this file covers what it leaves
dark: a release whose learner part has no usable cmi5.xml (ContentError mapped to 422 at
serve time), the AU-index 404s of the content and grade routes, the grade-attempt budget
(including its env fallback), and the abandon route — the statement it writes and the
session it ends, plus the same-tenant stranger it hides. The release is the committed
C105 bundle, mutated and re-digested like ``test_cov_e_releases_bundle.py`` does; the LRS
is ``MemoryLRS``. Nothing is stubbed.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
import uuid
from contextlib import contextmanager

import pytest
from _cmi5_kit import ABANDONED, BUNDLE, CATALOGUE, CMI5, CROSSWALK, EXT, MemoryLRS, quiz_answers
from app import xapi
from app.auth import CurrentUser, get_current_user
from app.cmi5 import lms
from app.cmi5.models import Cmi5Session
from app.course_releases import bundle as bundle_mod
from app.course_releases.bundle import PARTS, release_digest
from app.db import get_db
from app.enrollment import ensure_enrollment
from app.main import app as fastapi_app
from app.models import Course, User, UserRole

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


# -- fixtures (as in test_cmi5_lms.py) --------------------------------------------------------


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setenv("CMI5_LRS_AUTH", "YXUta2V5OmF1LXNlY3JldA==")
    monkeypatch.setenv("LRS_AUTH", "c2VydmVyOnNlY3JldA==")


@pytest.fixture
def lrs(monkeypatch):
    mem = MemoryLRS()
    monkeypatch.setattr(lms, "get_lms_backend", lambda: mem)
    return mem


@pytest.fixture
def release(client):
    """C105, uploaded and accepted by the dev admin (AUTH_DISABLED)."""
    client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", CROSSWALK.read_bytes(), "text/csv")})
    r = client.post("/courses/import-programme", files={"file": ("c.csv", CATALOGUE.read_bytes(), "text/csv")})
    assert r.status_code == 200, r.text
    r = client.post("/course-releases", files={"file": (BUNDLE.name, BUNDLE.read_bytes(), "application/gzip")})
    assert r.status_code in (200, 201), r.text
    rel = r.json()
    if rel["state"] == "candidate":
        acks = [a["id"] for a in rel["open_actions"]]
        r = client.post(f"/course-releases/{rel['id']}/accept", json={"acknowledge_actions": acks})
        assert r.status_code == 200, r.text
        rel = r.json()
    # A Student reaches a module only in a published course (catalogue courses import unpublished).
    db = client.app.dependency_overrides[get_db]().__next__()
    db.get(Course, uuid.UUID(rel["course_id"])).is_published = True
    db.commit()
    return rel


def _user(db, role=UserRole.student, tenant=DEV_TENANT) -> User:
    u = User(
        id=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Student",
        role=role,
        tenant_id=tenant,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def student(db_session, release):
    u = _user(db_session)
    ensure_enrollment(db_session, user_id=u.id, course_id=uuid.UUID(release["course_id"]), tenant_id=DEV_TENANT)
    db_session.commit()
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


def _launch(client, user, release, index=0, **body):
    with acting_as(user):
        r = client.post(f"/cmi5/releases/{release['id']}/aus/{index}/launch", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _grade(client, user, release, au_index=0, answers=None):
    key = dict(quiz_answers(au_index)) if answers is None else answers
    with acting_as(user):
        return client.post(f"/cmi5/releases/{release['id']}/aus/{au_index}/grade", json={"answers": key})


# -- a release that cannot serve its cmi5 package ----------------------------------------------


def _broken_tarball(*, drop=(), edit_files=None) -> bytes:
    """Rebuild C105 with members dropped/edited, recomputing every digest (see
    test_cov_e_releases_bundle.py) — the upload is valid, the package is not."""

    def untar(data: bytes) -> dict[str, bytes]:
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)), mode="r:") as tar:
            return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}

    def tar_of(members: dict[str, bytes]) -> bytes:
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:") as tar:
            for name in sorted(members):
                blob = members[name]
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(blob))
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
            gz.write(raw.getvalue())
        return buf.getvalue()

    members = untar(BUNDLE.read_bytes())
    for path in drop:
        members.pop(path)
    for path, blob in (edit_files or {}).items():
        members[path] = blob
    meta = json.loads(members["release.json"])
    for name in PARTS:
        part_files = {p[len(name) + 1 :]: b for p, b in members.items() if p.startswith(f"{name}/")}
        meta["parts"][name] = {
            "digest": bundle_mod.digest_files(part_files),
            "files": [
                {"path": rel, "sha256": hashlib.sha256(blob).hexdigest()} for rel, blob in sorted(part_files.items())
            ],
        }
    meta["release_digest"] = release_digest({n: meta["parts"][n]["digest"] for n in PARTS}, meta)
    members["release.json"] = json.dumps(meta).encode()
    return tar_of(members)


def _broken_release(client, **kwargs) -> dict:
    data = _broken_tarball(**kwargs)
    client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", CROSSWALK.read_bytes(), "text/csv")})
    r = client.post("/courses/import-programme", files={"file": ("c.csv", CATALOGUE.read_bytes(), "text/csv")})
    assert r.status_code == 200, r.text
    r = client.post("/course-releases", files={"file": ("broken.tar.gz", data, "application/gzip")})
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_a_release_without_cmi5_xml_reports_422_when_served(client):
    rel = _broken_release(client, drop=("learner/07-bundle/cmi5/cmi5.xml",))
    r = client.get(f"/cmi5/releases/{rel['id']}/cmi5.xml")
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == f"release {rel['id']} carries no 07-bundle/cmi5/cmi5.xml"


def test_a_release_with_unparseable_cmi5_xml_reports_422_when_served(client):
    rel = _broken_release(client, edit_files={"learner/07-bundle/cmi5/cmi5.xml": b"<courseStructure"})
    r = client.get(f"/cmi5/releases/{rel['id']}/structure")
    assert r.status_code == 422, r.text
    assert "not well-formed" in r.json()["detail"]


# -- AU indexes beyond the course ---------------------------------------------------------------


def test_content_for_an_au_beyond_the_course_is_not_found(client, release, student):
    with acting_as(student):
        for bad in (6, 99):
            r = client.get(f"/cmi5/releases/{release['id']}/aus/{bad}/content")
            assert r.status_code == 404, r.text
            assert r.json()["detail"] == f"no AU {bad}: the course has 6"


def test_grading_an_au_beyond_the_course_is_not_found(client, release, student):
    r = _grade(client, student, release, au_index=99, answers={})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "no AU 99: the course has 6"


# -- the grade-attempt budget -------------------------------------------------------------------


def test_a_broken_attempt_budget_falls_back_to_three(client, release, student, monkeypatch):
    monkeypatch.setenv("CMI5_GRADE_ATTEMPTS", "two")

    for _ in range(3):
        r = _grade(client, student, release)
        assert r.status_code == 200, r.text
        assert r.json()["scaled"] == 1.0
    r = _grade(client, student, release)
    assert r.status_code == 429, r.text
    assert r.json()["detail"] == "3 attempts at this quiz in the last 24 hours; try again later"


def test_the_attempt_budget_is_read_from_the_environment(client, release, student, monkeypatch):
    monkeypatch.setenv("CMI5_GRADE_ATTEMPTS", "1")

    assert _grade(client, student, release).status_code == 200
    r = _grade(client, student, release)
    assert r.status_code == 429, r.text
    assert r.json()["detail"] == "1 attempts at this quiz in the last 24 hours; try again later"


# -- abandon ------------------------------------------------------------------------------------


def test_abandon_writes_the_abandoned_statement_and_ends_the_session(client, lrs, release, student, db_session):
    out = _launch(client, student, release)

    with acting_as(student):
        r = client.post(f"/cmi5/sessions/{out['session_id']}/abandon")

    assert r.status_code == 204, r.text
    (abandoned,) = lrs.by_verb(ABANDONED)
    assert abandoned["actor"] == xapi.actor(student.id)
    assert abandoned["object"]["id"].endswith(f"/cmi5/releases/{release['id']}/au/0")
    assert abandoned["context"]["registration"] == out["registration"]
    assert {"id": CMI5} in abandoned["context"]["contextActivities"]["category"]
    assert abandoned["context"]["extensions"][EXT + "sessionid"] == out["session_id"]

    session = db_session.get(Cmi5Session, uuid.UUID(out["session_id"]))
    assert session.state == "abandoned"
    assert session.ended_at is not None


def test_a_same_tenant_stranger_cannot_find_the_session_to_abandon(client, lrs, release, student, db_session):
    out = _launch(client, student, release)
    stranger = _user(db_session)
    db_session.commit()

    with acting_as(stranger):
        r = client.post(f"/cmi5/sessions/{out['session_id']}/abandon")

    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "session not found"
    assert lrs.by_verb(ABANDONED) == []  # nothing was abandoned (the launch's own statement aside)
    assert db_session.get(Cmi5Session, uuid.UUID(out["session_id"])).state != "abandoned"
