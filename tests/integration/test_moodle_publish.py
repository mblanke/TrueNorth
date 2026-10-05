"""Publish ARC² releases into a real Moodle and play a student through them.

Runs against the disposable Moodle in infra/platform/docker/compose.moodle-test.yml (see
that file); skipped unless it is configured:

    MOODLE_TEST_URL        e.g. http://localhost:8093 (the Moodle's wwwroot)
    MOODLE_TEST_KEY        the private key whose public half the Moodle trusts (PEM file)
    MOODLE_TEST_CONTAINER  the Moodle container, for the student script (default
                           tn-moodle-test-moodle-1)

What it proves, end to end through app.course_publishing and local_truenorth:
  1. an accepted release is staged hidden, verified and activated as one visible course
     with sections, HTML pages, native quizzes (questions included) and, for a range
     module, an LTI lab link; the staging course is gone afterwards;
  2. a student enrols, opens a page, starts the quiz, resumes it, passes, and both the
     page and the quiz record completion and the grade;
  3. publishing the same release again changes nothing in Moodle;
  4. a second release with a changed question keeps the attempted quiz (hidden, with
     its grade) and adds the new one.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest
import yaml

URL = os.getenv("MOODLE_TEST_URL", "")
TENANT = uuid.UUID(os.getenv("MOODLE_TEST_TENANT", "7e57e57e-0000-4000-8000-000000000001"))
KEY = os.getenv("MOODLE_TEST_KEY", "")
CONTAINER = os.getenv("MOODLE_TEST_CONTAINER", "tn-moodle-test-moodle-1")
HERE = pathlib.Path(__file__).resolve().parent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (URL and KEY), reason="MOODLE_TEST_URL and MOODLE_TEST_KEY are not set"),
]


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """A real API database (SQLite) with the catalogue, a Moodle platform and a backend
    signing with the test key."""
    sys.path.insert(0, str(HERE.parent / "api"))
    from _release_kit import CATALOGUE, CROSSWALK
    from app import programme_ingest, qsp_ingest
    from app.db import Base
    from app.models import ExternalPlatform, IntegrationAuthType, Tenant
    from app.moodle_backends import LocalTrueNorthMoodle
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('db') / 'tn.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    tenant = Tenant(id=TENANT, name="t", slug=f"t-{uuid.uuid4().hex[:6]}")
    db.add(tenant)
    db.commit()
    qsp_ingest.import_crosswalk(db, CROSSWALK.read_text(encoding="utf-8"))
    programme_ingest.import_programme(db, CATALOGUE.read_text(encoding="utf-8"), tenant_id=tenant.id)
    platform = ExternalPlatform(
        name="Moodle test",
        slug="moodle-test",
        platform_type="moodle",
        base_url=URL,
        lti_issuer=URL,
        auth_type=IntegrationAuthType.lti13,
        tenant_id=tenant.id,
    )
    db.add(platform)
    db.commit()
    pem = pathlib.Path(KEY).read_text()
    backend = LocalTrueNorthMoodle(key_provider=lambda: (pem, "test-key"))
    return SimpleNamespace(db=db, tenant=tenant, platform=platform, backend=backend, tmp=tmp_path_factory)


def release(world, data: bytes):
    from app.course_releases import service

    rel, _ = service.create_candidate(world.db, data, tenant_id=world.tenant.id, user_id=None)
    service.accept(world.db, rel, user_id=None, acknowledge=[])
    world.db.commit()
    return rel


def publish(world, rel):
    from app.course_publishing import service

    pub, _ = service.request(world.db, rel, world.platform, user_id=None)
    world.db.commit()
    return service.run(world.db, pub, backend=world.backend)


def student(course_idnumber: str, username: str, *extra: str) -> dict:
    subprocess.run(
        ["docker", "cp", str(HERE / "moodle_student.php"), f"{CONTAINER}:/tmp/moodle_student.php"], check=True
    )
    out = subprocess.run(
        [
            "docker",
            "exec",
            CONTAINER,
            "php",
            "/tmp/moodle_student.php",
            f"--course={course_idnumber}",
            f"--username={username}",
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_publish_learn_republish_and_revise(world):
    from _release_kit import build, write_run
    from arc2 import release as arc_release

    first = release(world, build(world.tmp.mktemp("a"), range_ordinals=frozenset({6})))
    pub = publish(world, first)
    assert pub.state == "published", pub.error
    live_id = str(first.course_id)

    described = world.backend.describe_course(world.platform, live_id)
    assert described["visible"] == 1 and described["sections"] >= 6
    kinds = [a["type"] for a in described["activities"].values()]
    assert kinds.count("quiz") == 6 and kinds.count("lti") == 1 and kinds.count("page") == 6
    assert all(a["questions"] == 5 for a in described["activities"].values() if a["type"] == "quiz")
    assert all(a["content_length"] > 0 for a in described["activities"].values() if a["type"] == "page")
    assert world.backend.describe_course(world.platform, f"tn-stage:{first.id}") == {"exists": False}

    # A student starts the quiz, leaves, comes back, passes.
    username = f"student{uuid.uuid4().hex[:6]}"
    opened = student(live_id, username, "--leave-open")
    assert opened["attempt_state"] == "inprogress"
    done = student(live_id, username)
    assert done["resumed"] is True and done["attempt_state"] == "finished"
    assert done["quiz_grade"] == pytest.approx(100.0) and done["quiz_grade"] >= done["quiz_gradepass"]
    assert done["page_complete"] and done["quiz_complete"]

    # Publishing the same release again converges on what is there: same course, same activities.
    before = world.backend.describe_course(world.platform, live_id)
    from app.course_publishing import service

    pub.state = "requested"
    world.db.commit()
    assert service.run(world.db, pub, backend=world.backend).state == "published"
    after = world.backend.describe_course(world.platform, live_id)
    assert after["courseid"] == before["courseid"] and after["activities"] == before["activities"]

    # Release 2 rewords module 1's first question: a new quiz; the attempted one is kept, hidden.
    run = world.tmp.mktemp("b") / "arc2-iot-b"
    write_run(run, range_ordinals=frozenset({6}))
    course_file = run / "02-content/arc2-iot.yaml"
    doc = yaml.safe_load(course_file.read_text())
    doc["modules"][0]["quiz"]["questions"][0]["question"] += " (revised)"
    course_file.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    import tarfile
    from io import BytesIO

    with tarfile.open(fileobj=BytesIO(build(world.tmp.mktemp("c"), range_ordinals=frozenset({6}))), mode="r:gz") as t:
        meta = json.loads(t.extractfile("release.json").read())
    parts = arc_release.collect(run)
    meta.update(
        slug="arc2-iot-b", parts={n: {"digest": arc_release.digest_files(f), "files": f} for n, f in parts.items()}
    )
    meta["release_digest"] = arc_release.release_digest(meta)
    second = release(world, arc_release._tarball(run, meta))
    assert publish(world, second).state == "published"

    revised = world.backend.describe_course(world.platform, live_id)
    attempted = done["quiz"]
    assert revised["activities"][attempted]["visible"] == 0
    new = [
        k
        for k, a in revised["activities"].items()
        if a["type"] == "quiz" and a["visible"] and k.startswith("tn:mod_001:")
    ]
    assert len(new) == 1 and new[0] != attempted


def test_a_ticket_for_another_tenant_is_refused(world):
    """Every tenant's Moodle trusts the same TrueNorth key: the tenant in the ticket is what
    stops one tenant's job being replayed into another tenant's Moodle."""
    from app.moodle_backends import MoodleError

    other = SimpleNamespace(lti_issuer=URL, base_url=URL, tenant_id=uuid.uuid4())
    with pytest.raises(MoodleError, match="401|ticket refused"):
        world.backend.describe_course(other, str(uuid.uuid4()))


def test_script_in_authored_html_is_cleaned(world):
    """Pages and question text are purified before Moodle stores them."""
    hostile = '<h2>Safe</h2><img src=x onerror="alert(1)"><script>alert(2)</script><p>kept</p>'
    course_id = str(uuid.uuid4())
    payload = {
        "idnumber": course_id,
        "fullname": "XSS check",
        "shortname": f"xss-{course_id[:8]}",
        "visible": False,
        "category": {"idnumber": "tn-catalogue", "name": "TrueNorth courses"},
        "sections": [
            {
                "name": "1. x",
                "activities": [
                    {
                        "idnumber": "tn:mod_001:page:01",
                        "type": "page",
                        "name": "p",
                        "content": hostile,
                        "format": "html",
                    },
                    {
                        "idnumber": "tn:mod_001:quiz:x",
                        "type": "quiz",
                        "name": "q",
                        "questions": [{"text": hostile, "answers": ["a", "b"], "correct": [0]}],
                    },
                ],
            }
        ],
    }
    world.backend.upsert_course(world.platform, payload)
    php = (
        "<?php define('CLI_SCRIPT', true); require('/var/www/html/config.php');"
        f"$c=$DB->get_record('course',['idnumber'=>'{course_id}'],'*',MUST_EXIST);"
        "$p=$DB->get_field('page','content',['course'=>$c->id]);"
        "$q=$DB->get_field_sql('SELECT q.questiontext FROM {question} q JOIN {question_versions} v ON v.questionid=q.id"
        " JOIN {question_bank_entries} e ON e.id=v.questionbankentryid JOIN {question_categories} qc ON qc.id=e.questioncategoryid"
        " JOIN {context} ctx ON ctx.id=qc.contextid JOIN {course_modules} cm ON cm.id=ctx.instanceid AND ctx.contextlevel=70"
        " WHERE cm.course=?',[$c->id]);"
        "echo json_encode(['page'=>$p,'question'=>$q]);"
    )
    subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "sh", "-c", "cat > /tmp/xss.php"], input=php, text=True, check=True
    )
    out = json.loads(
        subprocess.run(
            ["docker", "exec", CONTAINER, "php", "/tmp/xss.php"], capture_output=True, text=True, check=True
        ).stdout
    )
    for text in out.values():
        assert "<script" not in text and "onerror" not in text and "kept" in text
