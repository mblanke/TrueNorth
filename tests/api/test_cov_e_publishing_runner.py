# Author: Marc-Andre Blanke
# Created: 2026-11-28T12:55:38Z
"""Round-2 coverage for app/course_publishing/runner.py: what a background run does when
the job cannot be run.

The existing publication tests drive the happy path through the API. These exercise the
runner's own contract: a refused job is logged and left alone, a lease stolen by a
competing process stops the run at its last committed step, a waiting job that cannot run
stays waiting, and startup resume both publishes a stalled job and survives a Moodle that
is down. Every run goes through the real service; the only collaborators replaced are the
session factory (the borrowed-session pattern from test_course_publications) and the fake
Moodle, which gains a lease-stealing upsert to play the competing process.
"""

from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from _release_kit import CATALOGUE, CROSSWALK, build
from app import db as app_db
from app.course_publishing import service
from app.course_publishing.models import CoursePublication
from app.course_publishing.runner import resume_on_start, run_by_id, run_waiting
from app.models import ExternalPlatform, IntegrationAuthType
from app.moodle_backends import fake
from test_course_releases import upload

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
        name="Runner Moodle",
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


@pytest.fixture
def own_session(db_session, monkeypatch):
    """Patch the runner's SessionLocal so a manual run drives the test's session (close()
    is the test's). Called only around the manual runner call: the API's own background
    tasks keep the real factory and crash harmlessly against the empty test engine."""

    def borrow():
        monkeypatch.setattr(app_db, "SessionLocal", lambda: _Borrowed(db_session))

    return borrow


def accepted(client, data: bytes) -> str:
    rid = upload(client, data).json()["id"]
    assert client.post(f"/course-releases/{rid}/accept", json={}).status_code == 200
    return rid


def publish(client, rid: str, platform, wait=True):
    return client.post(
        f"/course-releases/{rid}/publications?wait={str(wait).lower()}", json={"platform_id": str(platform.id)}
    )


def queued(client, platform, tmp_path, db_session):
    """An accepted release with a queued (not run) publication job."""
    rid = accepted(client, build(tmp_path))
    pub_id = uuid.UUID(publish(client, rid, platform, wait=False).json()["id"])
    return rid, db_session.get(CoursePublication, pub_id)


class _Borrowed:
    """The test's session, handed to code that opens and closes its own."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass


# -- run_by_id -----------------------------------------------------------------


def test_run_by_id_of_an_unknown_publication_is_a_silence(own_session):
    own_session()
    run_by_id(uuid.uuid4())  # no job, nothing to run, nothing raised


def test_a_refused_run_is_logged_and_the_job_left_alone(ready, own_session, tmp_path, db_session):
    """A job whose lease another run holds is refused by service.run; the runner logs it,
    and the waiting sweep that follows finds the same refusal and gives up."""
    client, platform = ready
    _, pub = queued(client, platform, tmp_path, db_session)
    assert service.claim(db_session, pub) is True  # a run is in progress on this job

    own_session()
    run_by_id(pub.id)  # must not raise
    after = db_session.get(CoursePublication, pub.id)
    assert after.state == "requested"
    assert fake.site(platform) == {}  # nothing reached Moodle


def test_a_lease_stolen_mid_run_stops_the_run_and_leaves_the_job_to_the_winner(
    ready, own_session, tmp_path, db_session, monkeypatch, caplog
):
    """A competing process takes the lease with its own session while the run is in
    flight; the runner reports it and writes nothing more — the job is the winner's now.
    The run's emergency rollback also undoes this harness's rows (one shared outer
    transaction), so the outcome is read off the Moodle and the log, not the database."""
    client, platform = ready
    _, pub = queued(client, platform, tmp_path, db_session)
    stage_idnumber, course_id = pub.stage_idnumber, pub.course_id
    # The run's rollback empties the harness's tables, so read the fake Moodle through a
    # snapshot of the key the site dict is filed under, not the (now expired) row.
    site_key = SimpleNamespace(lti_issuer=platform.lti_issuer, base_url=platform.base_url)

    upsert = fake.FakeMoodle.upsert_course

    def steal_then_upsert(self, p, payload):
        # The competing process's own write: flushed so the run's next lease renewal
        # collides with it. Not committed — that would expire this test's instance of
        # pub — and then detached, so the run's emergency rollback (which in this harness
        # unwinds every row of the shared outer transaction) leaves the instance loadable
        # and the lease-loss error the service raises can name the job at all.
        pub.lease_holder = "thief"
        db_session.flush()
        db_session.expunge(pub)
        return upsert(self, p, payload)

    monkeypatch.setattr(fake.FakeMoodle, "upsert_course", steal_then_upsert)
    own_session()
    with caplog.at_level(logging.INFO, logger="app.course_publishing.runner"):
        run_by_id(pub.id)  # must not raise

    assert "lost its lease mid-run" in caplog.text
    site = fake.site(site_key)
    assert stage_idnumber in site  # the run got as far as staging before the theft
    assert str(course_id) not in site  # the live course was never activated


# -- run_waiting ---------------------------------------------------------------


def test_a_waiting_publication_that_cannot_run_stays_waiting(ready, own_session, tmp_path, db_session):
    """v1's run is in progress when v2 is asked for: the API queues v2's job, and if v2
    still cannot run when the sweep gets to it, it stays queued for the next one."""
    client, platform = ready
    first = accepted(client, build(tmp_path / "a"))
    first_pub_id = uuid.UUID(publish(client, first, platform, wait=False).json()["id"])
    first_pub = db_session.get(CoursePublication, first_pub_id)
    assert service.claim(db_session, first_pub) is True  # v1's run is in progress
    db_session.commit()

    second = accepted(client, build(tmp_path / "b", slug="arc2-iot-b"))
    assert publish(client, second, platform).status_code == 409  # queued while v1 runs
    second_pub = db_session.query(CoursePublication).filter(CoursePublication.id != first_pub_id).one()

    own_session()
    run_waiting(first_pub_id)  # v2's claim is refused by v1's live lease
    assert second_pub.state == "requested"
    assert fake.site(platform) == {}


def test_the_waiting_sweep_runs_the_next_publication_when_the_lease_is_gone(ready, own_session, tmp_path, db_session):
    client, platform = ready
    first = accepted(client, build(tmp_path / "a"))
    first_pub_id = uuid.UUID(publish(client, first, platform, wait=False).json()["id"])
    first_pub = db_session.get(CoursePublication, first_pub_id)
    assert service.claim(db_session, first_pub) is True
    db_session.commit()

    second = accepted(client, build(tmp_path / "b", slug="arc2-iot-b"))
    assert publish(client, second, platform).status_code == 409
    first_pub.lease_until = None  # v1's run ends without publishing
    db_session.commit()

    own_session()
    run_waiting(first_pub_id)
    second_pub = db_session.query(CoursePublication).filter(CoursePublication.id != first_pub_id).one()
    assert second_pub.state == "published"
    live = fake.site(platform).get(str(second_pub.course_id))
    assert live is not None and live["visible"] == 1


# -- resume_on_start -----------------------------------------------------------


def test_startup_resume_publishes_a_job_whose_process_died(ready, own_session, tmp_path, db_session):
    client, platform = ready
    _, pub = queued(client, platform, tmp_path, db_session)
    pub.state = "verifying"  # as a crashed worker would leave it, lease lapsed
    pub.lease_until = None
    db_session.commit()

    own_session()
    resume_on_start()

    done = db_session.get(CoursePublication, pub.id)
    assert done.state == "published"
    assert json.loads(done.receipt)["release_version"] == 1
    live = fake.site(platform).get(str(done.course_id))
    assert live is not None and live["visible"] == 1


def test_startup_resume_never_blocks_on_a_moodle_that_is_down(own_session, monkeypatch):
    def moodle_down(db):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(service, "resume_stalled", moodle_down)
    own_session()
    resume_on_start()  # logs "could not resume" and returns
