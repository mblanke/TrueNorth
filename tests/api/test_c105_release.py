"""The committed C105 release (content/releases/) is the one the programme owner accepted,
and the API takes it end to end: upload → candidate → accept → the catalogue course
delivers its six theory modules.

The bundle was built from the accepted ARC² run, not for this test:

    cd tools && python -m arc2.release build ../build/arc2/arc2-c105-foundations
    # -> release d0e83b9f6c1f  C105  (learner 76, platform 9, instructor 7)

``arc2.check check`` on that run reports QA pass (cycle 0/3) with both gates accepted, and a
rebuild with the current tools/arc2/release.py is byte-identical (the sha256 below), so the
tarball is reproducible from the run. tests/integration/test_content_release_flow.py runs
the same flow against a live API.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml
from _release_kit import CATALOGUE, CROSSWALK
from app.course_releases import bundle as bundle_mod
from app.models import Course, CourseModule
from arc2 import release as arc_release
from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "content/releases/c105-foundations-d0e83b9f6c1f.tar.gz"
BUNDLE_SHA256 = "1c4a9c0b7abe8d6a4ed9f688f1656af803c4d3d38ceadb44519dcdf6e469cf92"
RELEASE_DIGEST = "d0e83b9f6c1f084d81ec2eff6c3222441e68ef8ce92fbd98d2e7ab7a1f265c71"
SCHEMA = json.loads((ROOT / "docs/interfaces/course-content.schema.json").read_text(encoding="utf-8"))
MODULE_TITLES = [
    "Security Principles and the CIA Triad",
    "Threat Actors and Attack Surfaces",
    "Access Control and Identity",
    "Applied Cryptography Concepts",
    "Security Governance and Frameworks",
    "Security Operations and the Analyst Role",
]


def _data() -> bytes:
    return BUNDLE.read_bytes()


def _parsed() -> bundle_mod.Bundle:
    return bundle_mod.parse(_data())


def _course_yaml() -> dict:
    parsed = _parsed()
    return yaml.safe_load(parsed.files["platform"][parsed.meta["course_yaml"]].decode("utf-8"))


@pytest.fixture
def catalogue(client):
    client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", CROSSWALK.read_bytes(), "text/csv")})
    resp = client.post("/courses/import-programme", files={"file": ("c.csv", CATALOGUE.read_bytes(), "text/csv")})
    assert resp.status_code == 200, resp.text
    return client


def _upload(client):
    return client.post("/course-releases", files={"file": (BUNDLE.name, _data(), "application/gzip")})


def _c105(db) -> Course:
    for c in db.query(Course).all():
        if json.loads(c.course_meta or "{}").get("course_code") == "C105":
            return c
    raise AssertionError("C105 not imported")


# -- the artefact ------------------------------------------------------------------


def test_the_bundle_is_the_accepted_release():
    assert hashlib.sha256(_data()).hexdigest() == BUNDLE_SHA256
    meta = _parsed().meta
    assert meta["release_digest"] == RELEASE_DIGEST
    assert arc_release.release_digest(meta) == RELEASE_DIGEST
    assert meta["catalogue_code"] == "C105" and meta["arc2_code"] == "ARC2-C105"
    assert set(meta["activities"].values()) == {"theory"} and len(meta["activities"]) == 6


def test_its_course_file_conforms_to_the_course_schema():
    errors = [e.message for e in Draft7Validator(SCHEMA).iter_errors(_course_yaml())]
    assert errors == []


def test_its_course_file_is_still_a_proposal():
    """Acceptance and publication are release state; the file never claims either."""
    doc = _course_yaml()
    assert doc["status"] == "proposed" and doc["is_published"] is False and doc["provenance"] == "unsourced"
    assert [m["title"] for m in doc["modules"]] == MODULE_TITLES


def test_the_learner_part_holds_no_instructor_material():
    files = _parsed().meta["parts"]["learner"]["files"]
    assert files and not arc_release.learner_leaks([f["path"] for f in files])


def test_a_flipped_byte_is_refused():
    data = bytearray(_data())
    data[len(data) // 2] ^= 0xFF
    with pytest.raises(bundle_mod.BundleError):
        bundle_mod.parse(bytes(data))


# -- the flow, in process ----------------------------------------------------------


def test_upload_accept_and_the_course_lists_its_modules(catalogue, db_session):
    resp = _upload(catalogue)
    assert resp.status_code == 201, resp.text
    release = resp.json()
    assert release["state"] == "candidate" and release["release_digest"] == RELEASE_DIGEST
    actions = [a["id"] for a in release["open_actions"]]
    assert len(actions) == 6

    refused = catalogue.post(f"/course-releases/{release['id']}/accept", json={})
    assert refused.status_code == 409 and "acknowledge the open actions" in refused.json()["detail"]

    ok = catalogue.post(
        f"/course-releases/{release['id']}/accept", json={"acknowledge_actions": actions, "notes": "test"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["state"] == "accepted"

    course = _c105(db_session)
    listed = catalogue.get("/courses", params={"limit": 200}).json()["items"]
    assert str(course.id) in {c["id"] for c in listed}
    modules = sorted(db_session.query(CourseModule).filter_by(course_id=course.id).all(), key=lambda m: m.ordinal)
    assert [m.title for m in modules] == MODULE_TITLES
    assert {json.loads(m.content_ref)["activity"] for m in modules} == {"theory"}
    status = catalogue.get(f"/course-releases/courses/{course.id}").json()
    assert status["legacy"] is False and status["active_version"] == 1


def test_the_learner_download_is_pages_and_cmi5_only(catalogue):
    rid = _upload(catalogue).json()["id"]
    resp = catalogue.get(f"/course-releases/{rid}/learner-bundle")
    assert resp.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        names = tar.getnames()
    assert names and all(n.startswith(("02-content/mod_", "07-bundle/cmi5/")) for n in names)
    assert not arc_release.learner_leaks(names)


# -- PostgreSQL (skipped without TEST_POSTGRES_ADMIN_URL) ----------------------------


def test_the_first_upload_is_stored_on_postgresql(postgres_engine):
    """The API's sessions do not autoflush (app.db.SessionLocal). The release and its
    blob were flushed together and the unit of work, with no relationship() to order
    them, inserted the release first: PostgreSQL refused the foreign key and the API
    answered the very first upload with 409 "another upload ... at the same moment".
    SQLite does not enforce the key, so only the integration stack saw it."""
    import uuid

    from app import programme_ingest, qsp_ingest
    from app.course_releases import service
    from app.course_releases.models import CourseRelease, CourseReleaseBlob
    from app.models import Tenant
    from sqlalchemy.orm import Session

    with Session(postgres_engine, autoflush=False, expire_on_commit=False) as s:
        tenant = Tenant(name="t", slug=f"t-{uuid.uuid4().hex[:6]}")
        s.add(tenant)
        s.commit()
        qsp_ingest.import_crosswalk(s, CROSSWALK.read_text(encoding="utf-8"), tenant_id=tenant.id)
        programme_ingest.import_programme(s, CATALOGUE.read_text(encoding="utf-8"), tenant_id=tenant.id)
        s.commit()

        release, created = service.create_candidate(s, _data(), tenant_id=tenant.id, user_id=None)
        s.commit()
        assert created and release.version == 1 and release.release_digest == RELEASE_DIGEST
        assert s.get(CourseReleaseBlob, BUNDLE_SHA256) is not None

        again, created = service.create_candidate(s, _data(), tenant_id=tenant.id, user_id=None)
        assert not created and again.id == release.id
        assert s.query(CourseRelease).count() == 1
