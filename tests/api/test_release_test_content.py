"""Test content travels with a release (adversarial review of #130/#136).

A release whose ARC² gates a test host's runner accepted (``auto_accepted`` in release.json,
under the digest) is accepted and published only where ``TN_TEST_HOST`` is on, and there it
is labelled where Students see it: the course page (``test_content`` in the outline) and
the Moodle course name and summary. The upload is checked against the run's manifest and
against the runner's own job records (the authority: a job can write its manifest).
"""

from __future__ import annotations

import json
import uuid

import pytest
from _release_kit import build
from app.course_releases.test_content import LABEL
from app.moodle_backends import fake
from test_course_publications import live, publish, ready  # noqa: F401 - fixture
from test_course_releases import _course, catalogue, upload  # noqa: F401 - fixture

AUTO = ("outline", "preview")


@pytest.fixture(autouse=True)
def _no_test_host(monkeypatch):
    monkeypatch.delenv("TN_TEST_HOST", raising=False)
    fake.reset()
    yield
    fake.reset()


class TestUpload:
    def test_release_json_says_test_content_and_the_api_shows_it(self, catalogue, tmp_path):  # noqa: F811
        body = upload(catalogue, build(tmp_path, auto_gates=AUTO)).json()
        assert body["auto_accepted"] is True
        assert upload(catalogue, build(tmp_path / "b", slug="arc2-iot-b")).json()["auto_accepted"] is False

    def test_a_release_that_hides_an_auto_accepted_gate_is_refused(self, catalogue, tmp_path):  # noqa: F811
        resp = upload(catalogue, build(tmp_path, auto_gates=("preview",), auto_accepted=False))
        assert resp.status_code == 422
        assert "accepted by the test host's runner" in resp.json()["detail"]

    def test_the_runners_record_is_the_authority_not_the_manifest(self, catalogue, tmp_path, monkeypatch):  # noqa: F811
        """The job rewrote its manifest to drop accepted_by; the runner's _jobs record of
        its own acceptance still marks the run, and the upload is refused."""
        runs = tmp_path / "runs"
        (runs / "_jobs").mkdir(parents=True)
        (runs / "_jobs" / "auto0001.json").write_text(
            json.dumps({"id": "auto0001", "slug": "arc2-iot", "state": "done", "auto_accept": {"gate": "outline"}})
        )
        monkeypatch.setenv("ARC2_RUNS_DIR", str(runs))
        monkeypatch.setenv("ARC2_STUDIO_ENABLED", "true")
        resp = upload(catalogue, build(tmp_path))
        assert resp.status_code == 422 and "rebuild the release" in resp.json()["detail"]
        assert upload(catalogue, build(tmp_path / "x", slug="arc2-iot-x")).status_code == 201  # another run


class TestAcceptAndLabel:
    def test_off_a_test_host_test_content_is_never_accepted(self, catalogue, tmp_path):  # noqa: F811
        rid = upload(catalogue, build(tmp_path, auto_gates=AUTO)).json()["id"]
        resp = catalogue.post(f"/course-releases/{rid}/accept", json={})
        assert resp.status_code == 409 and "TN_TEST_HOST" in resp.json()["detail"]
        assert catalogue.get(f"/course-releases/{rid}").json()["state"] == "candidate"

    def test_on_a_test_host_it_is_accepted_and_the_course_page_says_so(
        self, catalogue, tmp_path, db_session, monkeypatch  # noqa: F811
    ):
        monkeypatch.setenv("TN_TEST_HOST", "true")
        rid = upload(catalogue, build(tmp_path, auto_gates=AUTO)).json()["id"]
        assert catalogue.post(f"/course-releases/{rid}/accept", json={}).status_code == 200
        course = _course(db_session)
        db_session.refresh(course)
        label = json.loads(course.course_meta)["test_content"]
        assert label["label"] == LABEL and label["release_id"] == rid and label["gates"] == ["outline", "preview"]
        assert catalogue.get(f"/courses/{course.id}/outline").json()["test_content"] == LABEL

        # A reviewed release replaces it: the label goes with it.
        second = upload(catalogue, build(tmp_path / "b", slug="arc2-iot-b", title_suffix=" (rev)")).json()["id"]
        assert catalogue.post(f"/course-releases/{second}/accept", json={}).status_code == 200
        db_session.refresh(course)
        assert "test_content" not in json.loads(course.course_meta)
        assert catalogue.get(f"/courses/{course.id}/outline").json()["test_content"] is None


class TestPublish:
    def test_on_a_test_host_moodle_shows_the_label(self, ready, tmp_path, db_session, monkeypatch):  # noqa: F811
        client, platform = ready
        monkeypatch.setenv("TN_TEST_HOST", "1")
        rid = upload(client, build(tmp_path, auto_gates=AUTO)).json()["id"]
        assert client.post(f"/course-releases/{rid}/accept", json={}).status_code == 200
        assert publish(client, rid, platform).json()["state"] == "published"
        payload = live(platform, db_session, rid)["payload"]
        assert LABEL in payload["fullname"] and payload["summary"].startswith("TEST CONTENT")

    def test_a_reviewed_release_is_not_labelled(self, ready, tmp_path, db_session):  # noqa: F811
        client, platform = ready
        rid = upload(client, build(tmp_path)).json()["id"]
        assert client.post(f"/course-releases/{rid}/accept", json={}).status_code == 200
        assert publish(client, rid, platform).json()["state"] == "published"
        payload = live(platform, db_session, rid)["payload"]
        assert "TEST CONTENT" not in payload["fullname"] + payload["summary"]

    def test_test_content_accepted_on_a_test_host_does_not_publish_once_the_flag_is_off(
        self, ready, tmp_path, monkeypatch, db_session  # noqa: F811
    ):
        client, platform = ready
        monkeypatch.setenv("TN_TEST_HOST", "true")
        rid = upload(client, build(tmp_path, auto_gates=AUTO)).json()["id"]
        assert client.post(f"/course-releases/{rid}/accept", json={}).status_code == 200
        monkeypatch.setenv("TN_TEST_HOST", "false")
        resp = publish(client, rid, platform)
        assert resp.status_code == 409 and "TN_TEST_HOST" in resp.json()["detail"]
        assert not fake.calls()

    def test_a_queued_job_fails_rather_than_publish_off_a_test_host(self, ready, tmp_path, monkeypatch, db_session):  # noqa: F811
        from app.course_publishing import service
        from app.course_publishing.models import CoursePublication
        from app.course_releases.models import CourseRelease

        client, platform = ready
        monkeypatch.setenv("TN_TEST_HOST", "true")
        rid = upload(client, build(tmp_path, auto_gates=AUTO)).json()["id"]
        assert client.post(f"/course-releases/{rid}/accept", json={}).status_code == 200
        pub, _ = service.request(
            db_session, db_session.get(CourseRelease, uuid.UUID(rid)), platform, user_id=None
        )
        db_session.commit()
        monkeypatch.setenv("TN_TEST_HOST", "")
        done = service.run(db_session, db_session.get(CoursePublication, pub.id), backend=fake.FakeMoodle())
        assert done.state == "failed" and "TN_TEST_HOST" in done.error
        assert not fake.calls()
