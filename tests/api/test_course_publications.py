"""Publishing accepted releases to Moodle, against the in-memory Moodle (app.moodle_backends.fake).

Staging is hidden and verified before anything goes live; an interruption at any step
leaves the live course as it was and a retry converges without duplicates; a second
release supersedes the first in place and a quiz students attempted is hidden, not lost.
"""

from __future__ import annotations

import uuid

import pytest
from _release_kit import CATALOGUE, CROSSWALK, build
from app.course_publishing.models import CoursePublication
from app.models import ExternalPlatform, IntegrationAuthType, UserRole
from app.moodle_backends import fake
from test_course_releases import OTHER_TENANT, acting_as, upload

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture(autouse=True)
def _clean_fake():
    fake.reset()
    yield
    fake.reset()


@pytest.fixture
def ready(client, db_session):
    client.post("/qsp/import-crosswalk", files={"file": ("crosswalk.csv", CROSSWALK.read_bytes(), "text/csv")})
    client.post("/courses/import-programme", files={"file": ("c.csv", CATALOGUE.read_bytes(), "text/csv")})
    platform = ExternalPlatform(
        name="Test Moodle",
        slug=f"moodle-{uuid.uuid4().hex[:6]}",
        platform_type="fake",
        base_url="http://moodle.invalid",
        lti_issuer="http://moodle.invalid",
        auth_type=IntegrationAuthType.lti13,
        tenant_id=DEV_TENANT,
    )
    db_session.add(platform)
    db_session.commit()
    return client, platform


def accepted(client, data: bytes) -> str:
    rid = upload(client, data).json()["id"]
    assert client.post(f"/course-releases/{rid}/accept", json={}).status_code == 200
    return rid


def publish(client, rid: str, platform, wait=True):
    return client.post(
        f"/course-releases/{rid}/publications?wait={str(wait).lower()}", json={"platform_id": str(platform.id)}
    )


def live(platform, db_session, rid):
    from app.course_releases.models import CourseRelease

    course_id = str(db_session.get(CourseRelease, uuid.UUID(rid)).course_id)
    return fake.site(platform).get(course_id)


class TestPublish:
    def test_a_theory_release_goes_live_through_a_hidden_stage(self, ready, tmp_path, db_session):
        client, platform = ready
        rid = accepted(client, build(tmp_path))
        resp = publish(client, rid, platform)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["state"] == "published", body["error"]
        ops = [op for op, _ in fake.calls()]
        assert ops == ["upsert_course", "describe_course", "upsert_course", "describe_course", "delete_stage"]
        stage_id = fake.calls()[0][1]
        assert stage_id == f"tn-stage:{rid}"
        course = live(platform, db_session, rid)
        assert course["visible"] == 1 and course["sections"] == 6
        kinds = sorted({a["type"] for a in course["activities"].values()})
        assert kinds == ["page", "quiz"]  # theory: no lab links
        assert stage_id not in fake.site(platform)
        assert body["receipt"]["moodle_course_id"] == course["courseid"]
        assert body["receipt"]["release_version"] == 1

    def test_a_range_module_gets_a_lab_link(self, ready, tmp_path, db_session):
        client, platform = ready
        rid = accepted(client, build(tmp_path, range_ordinals=frozenset({2})))
        assert publish(client, rid, platform).json()["state"] == "published"
        labs = [k for k, a in live(platform, db_session, rid)["activities"].items() if a["type"] == "lti"]
        assert labs == ["tn:mod_002:lab"]
        payload = live(platform, db_session, rid)["payload"]
        lab = next(a for s in payload["sections"] for a in s["activities"] if a["type"] == "lti")
        from app.course_releases.models import CourseRelease

        course_id = db_session.get(CourseRelease, uuid.UUID(rid)).course_id
        assert lab["resource"] == f"lab:{course_id}:mod_002"  # the course, not the release

    def test_asking_again_returns_the_same_job(self, ready, tmp_path):
        client, platform = ready
        rid = accepted(client, build(tmp_path))
        first = publish(client, rid, platform).json()
        again = publish(client, rid, platform)
        assert again.status_code == 200 and again.json()["id"] == first["id"]
        assert [op for op, _ in fake.calls()].count("upsert_course") == 2  # not run again

    def test_only_an_accepted_release_publishes(self, ready, tmp_path):
        client, platform = ready
        rid = upload(client, build(tmp_path)).json()["id"]
        resp = publish(client, rid, platform)
        assert resp.status_code == 409 and "only the accepted release publishes" in resp.json()["detail"]


class TestInterruption:
    @pytest.mark.parametrize("op", ["upsert_course", "describe_course"])
    def test_a_failure_anywhere_is_recorded_and_a_retry_converges(self, ready, tmp_path, db_session, op):
        client, platform = ready
        rid = accepted(client, build(tmp_path))
        fake.fail_next(op)
        failed = publish(client, rid, platform).json()
        assert failed["state"] == "failed" and "injected failure" in failed["error"]
        retried = client.post(f"/course-publications/{failed['id']}/retry?wait=true").json()
        assert retried["state"] == "published" and retried["attempts"] == 2
        from app.course_releases.models import CourseRelease

        course_id = str(db_session.get(CourseRelease, uuid.UUID(rid)).course_id)
        assert list(fake.site(platform)) == [course_id]  # one live course, no stage left over
        expected = sum(len(s["activities"]) for s in live(platform, db_session, rid)["payload"]["sections"])
        assert len(live(platform, db_session, rid)["activities"]) == expected  # no duplicates

    def test_a_stage_left_behind_after_activation_is_a_warning_not_a_failure(self, ready, tmp_path):
        client, platform = ready
        rid = accepted(client, build(tmp_path))
        fake.fail_next("delete_stage")
        body = publish(client, rid, platform).json()
        assert body["state"] == "published"
        assert "was not deleted" in body["receipt"]["warnings"][0]

    def test_a_failed_stage_never_touches_the_live_course(self, ready, tmp_path, db_session):
        client, platform = ready
        first = accepted(client, build(tmp_path / "a"))
        publish(client, first, platform)
        before = live(platform, db_session, first)["payload"]
        second = accepted(client, build(tmp_path / "b", title_suffix=" (rev)", slug="arc2-iot-b"))
        fake.fail_next("describe_course")
        assert publish(client, second, platform).json()["state"] == "failed"
        assert live(platform, db_session, first)["payload"] == before  # students still on v1

    def test_retrying_an_old_release_never_rolls_moodle_back(self, ready, tmp_path, db_session):
        client, platform = ready
        v1 = accepted(client, build(tmp_path / "a"))
        fake.fail_next("upsert_course")
        p1 = publish(client, v1, platform).json()
        assert p1["state"] == "failed"
        v2 = accepted(client, build(tmp_path / "b", title_suffix=" (rev)", slug="arc2-iot-b"))
        assert publish(client, v2, platform).json()["state"] == "published"
        refused = client.post(f"/course-publications/{p1['id']}/retry?wait=true")
        assert refused.status_code == 409 and "publish that one" in refused.json()["detail"]
        assert "Release v2" in live(platform, db_session, v2)["payload"]["summary"]

    def test_a_resumed_job_of_a_superseded_release_does_not_run(self, ready, tmp_path, db_session):
        from app.course_publishing import service

        client, platform = ready
        v1 = accepted(client, build(tmp_path / "a"))
        p1 = publish(client, v1, platform, wait=False).json()["id"]
        accepted(client, build(tmp_path / "b", title_suffix=" (rev)", slug="arc2-iot-b"))
        pub = db_session.get(CoursePublication, uuid.UUID(p1))
        pub.state, pub.lease_until = "staging", None
        db_session.commit()
        service.resume_stalled(db_session)
        assert db_session.get(CoursePublication, uuid.UUID(p1)).state == "superseded"
        assert not fake.site(platform)  # nothing was pushed for v1

    def test_the_background_runner_publishes(self, ready, tmp_path, db_session, monkeypatch):
        """wait=false hands the job to run_by_id, which opens its own session."""
        from app import db as app_db
        from app.course_publishing.runner import run_by_id

        client, platform = ready
        rid = accepted(client, build(tmp_path))
        pub_id = publish(client, rid, platform, wait=False).json()["id"]
        monkeypatch.setattr(app_db, "SessionLocal", lambda: _Borrowed(db_session))
        run_by_id(uuid.UUID(pub_id))
        assert db_session.get(CoursePublication, uuid.UUID(pub_id)).state == "published"

    def test_a_job_whose_process_died_is_resumed(self, ready, tmp_path, db_session):
        from app.course_publishing import service

        client, platform = ready
        rid = accepted(client, build(tmp_path))
        pub_id = publish(client, rid, platform, wait=False).json()["id"]
        pub = db_session.get(CoursePublication, uuid.UUID(pub_id))
        pub.state = "verifying"  # as a crashed worker would leave it, lease lapsed
        pub.lease_until = None
        db_session.commit()
        assert service.resume_stalled(db_session) == 1
        assert db_session.get(CoursePublication, uuid.UUID(pub_id)).state == "published"

    def test_a_live_lease_blocks_a_second_runner(self, ready, tmp_path, db_session):
        from app.course_publishing import service

        client, platform = ready
        rid = accepted(client, build(tmp_path))
        pub_id = publish(client, rid, platform, wait=False).json()["id"]
        pub = db_session.get(CoursePublication, uuid.UUID(pub_id))
        assert service.claim(db_session, pub) is True
        with pytest.raises(service.PublishRefusedError, match="already running"):
            service.run(db_session, pub)


class TestNewRelease:
    def test_a_second_release_supersedes_and_keeps_attempted_quizzes(self, ready, tmp_path, db_session):
        client, platform = ready
        first = accepted(client, build(tmp_path / "a"))
        first_pub = publish(client, first, platform).json()
        course = live(platform, db_session, first)
        quiz1 = next(k for k, a in course["activities"].items() if a["type"] == "quiz" and k.startswith("tn:mod_001:"))
        fake.record_attempt(platform, next(k for k in fake.site(platform)), quiz1)

        second_data = build(tmp_path / "b", slug="arc2-iot-b")
        # change module 1's first question so its quiz is a new one
        second = accepted(client, _edit_question(second_data))
        second_pub = publish(client, second, platform).json()
        assert second_pub["state"] == "published"
        assert client.get(f"/course-publications/{first_pub['id']}").json()["state"] == "superseded"
        course = live(platform, db_session, second)
        assert course["activities"][quiz1]["visible"] == 0  # attempted: hidden, kept
        new_quiz = [
            k
            for k, a in course["activities"].items()
            if a["type"] == "quiz" and k.startswith("tn:mod_001:") and k != quiz1
        ]
        assert len(new_quiz) == 1 and course["activities"][new_quiz[0]]["visible"] == 1


class TestAccess:
    def test_students_cannot_publish(self, ready, tmp_path):
        client, platform = ready
        rid = accepted(client, build(tmp_path))
        with acting_as(UserRole.student):
            assert publish(client, rid, platform).status_code == 403

    def test_another_tenants_platform_or_release_is_not_found(self, ready, tmp_path):
        client, platform = ready
        rid = accepted(client, build(tmp_path))
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            assert publish(client, rid, platform).status_code == 404

    def test_a_platform_that_cannot_publish_is_refused(self, ready, tmp_path, db_session):
        client, platform = ready
        platform.platform_type = "offsec"
        db_session.commit()
        rid = accepted(client, build(tmp_path))
        resp = publish(client, rid, platform)
        assert resp.status_code == 422 and "cannot receive course publications" in resp.json()["detail"]


def test_supplied_evidence_becomes_a_download_and_a_wrong_file_fails_verification(tmp_path):
    from app.course_publishing import payload as payload_mod
    from app.course_publishing.service import _verify
    from app.course_releases.bundle import parse

    evidence = {
        "security-events.json": '{"EventID": 4624}\n',
        "index.html": "<p>not a page</p>",
        "ntuser/ntuser-a.reg": "Windows Registry Editor Version 5.00\n",
    }
    bundle = parse(build(tmp_path, evidence=evidence))
    built = payload_mod.build(
        bundle, release_id=uuid.uuid4(), course_id=uuid.uuid4(), idnumber=str(uuid.uuid4()),
        visible=True, category={"idnumber": "tn-catalogue", "name": "Catalogue"}, version=1,
    )
    first = built["sections"][0]["activities"]
    assert [a["type"] for a in first].count("page") == 1  # evidence HTML is a file, not a page
    files = {a["name"]: a for a in first if a["type"] == "resource"}
    assert set(files) == set(evidence)
    assert files["security-events.json"]["idnumber"].startswith("tn:mod_001:file:")
    assert files["ntuser/ntuser-a.reg"]["filename"] == "ntuser_ntuser-a.reg"  # subfolders are released too

    want = payload_mod.expected(built)
    described = {
        "exists": True, "visible": 1, "sections": len(built["sections"]),
        "activities": {
            idn: {"type": w["type"], "visible": 1, "section": w["section"], "questions": w.get("questions"),
                  "sha1": w.get("sha1")}
            for idn, w in want.items()
        },
    }
    assert _verify(described, want, len(built["sections"]), visible=True) == []
    described["activities"][files["index.html"]["idnumber"]]["sha1"] = "0" * 40
    assert _verify(described, want, len(built["sections"]), visible=True) == [
        f"{files['index.html']['idnumber']} holds a different file than the release"
    ]


def _edit_question(data: bytes) -> bytes:
    """Rebuild a release with module 1's first question reworded (digests recomputed by
    the tool, so the release is valid)."""
    import tempfile
    from pathlib import Path

    import yaml
    from _release_kit import write_run
    from arc2 import release as arc_release

    root = Path(tempfile.mkdtemp())
    run = root / "arc2-iot-c"
    write_run(run)
    course = run / "02-content/arc2-iot.yaml"
    doc = yaml.safe_load(course.read_text())
    doc["modules"][0]["quiz"]["questions"][0]["question"] += " (revised)"
    course.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    with __import__("tarfile").open(fileobj=__import__("io").BytesIO(data), mode="r:gz") as tar:
        import json

        meta = json.loads(tar.extractfile("release.json").read())
    parts = arc_release.collect(run)
    meta["slug"] = "arc2-iot-c"
    meta["parts"] = {n: {"digest": arc_release.digest_files(f), "files": f} for n, f in parts.items()}
    meta["release_digest"] = arc_release.release_digest(meta)
    return arc_release._tarball(run, meta)


class _Borrowed:
    """The test's session, handed to code that opens and closes its own."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass


def test_sync_tickets_name_the_platform_tenant():
    """Every tenant's Moodle trusts one TrueNorth key; the ticket's tid is what a Moodle
    checks against the tenant it serves (local_truenorth ticket.php)."""
    from types import SimpleNamespace

    import httpx
    import jwt as pyjwt
    from app.moodle_backends import LocalTrueNorthMoodle
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["claims"] = pyjwt.decode(request.headers["authorization"].split()[1], options={"verify_signature": False})
        seen["host"] = request.headers["host"]
        return httpx.Response(200, json={"ok": True, "exists": False})

    tenant = uuid.uuid4()
    backend = LocalTrueNorthMoodle(key_provider=lambda: (pem.decode(), "k"), transport=httpx.MockTransport(handler))
    site = SimpleNamespace(lti_issuer="https://moodle.example", base_url="http://moodle-node:8080", tenant_id=tenant)
    assert backend.describe_course(site, str(uuid.uuid4())) == {"exists": False}
    assert seen["claims"]["tid"] == str(tenant) and seen["claims"]["typ"] == "sync"
    assert seen["claims"]["aud"] == "https://moodle.example" and seen["host"] == "moodle.example"
