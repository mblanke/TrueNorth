"""A real Moodle launches a TrueNorth cmi5 AU over LTI 1.3 and gets the grade (AGS).

Runs against the disposable Moodle in infra/platform/docker/compose.moodle-test.yml, with
the same settings as tests/integration/test_moodle_publish.py (skipped unless set):

    MOODLE_TEST_URL        the Moodle's wwwroot, e.g. http://localhost:8093
    MOODLE_TEST_KEY        the private key whose public half the Moodle trusts (PEM file);
                           here it is TrueNorth's LTI tool key
    MOODLE_TEST_CONTAINER  the Moodle container (default tn-moodle-test-moodle-1)

What runs for real: Moodle (its External tool activity, OIDC login, signed id_token, AGS
token endpoint and score service, gradebook) and TrueNorth's API in-process (TestClient,
SQLite). The browser is this test: it carries Moodle's forms to TrueNorth's routes
(``TN_TOOL_URL`` in the compose file is only the address Moodle writes in its forms; nothing
listens there). The LRS is the in-memory one (tests/api/_cmi5_kit.py); the real-LRS round
trip is tests/integration/test_cmi5_lrs.py.

1. A Moodle Student opens the activity (custom ``resource=cmi5:<release>:0``, as a
   deep-linked cmi5 content item sets it). Moodle's login and launch go through TrueNorth's
   /lti/login and /lti/launch; the Student TrueNorth knows for that Moodle account is
   handed a session (/lti/session) whose target is the AU launcher.
2. With that session the Student launches the AU (TrueNorth as the cmi5 LMS), has the quiz
   marked (4 of 5), and the AU reports passed with that score.
3. TrueNorth posts the mark to the activity's line item; Moodle's gradebook shows 80/100.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import uuid
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

URL = os.getenv("MOODLE_TEST_URL", "").rstrip("/")
TENANT = uuid.UUID(os.getenv("MOODLE_TEST_TENANT", "7e57e57e-0000-4000-8000-000000000001"))
KEY = os.getenv("MOODLE_TEST_KEY", "")
CONTAINER = os.getenv("MOODLE_TEST_CONTAINER", "tn-moodle-test-moodle-1")
HERE = pathlib.Path(__file__).resolve().parent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (URL and KEY), reason="MOODLE_TEST_URL and MOODLE_TEST_KEY are not set"),
]


class _Form(HTMLParser):
    """The first form of a page: its action and its inputs."""

    def __init__(self):
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._in = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and self.action is None:
            self.action, self._in = a.get("action", ""), True
        elif tag == "input" and self._in and a.get("name"):
            self.fields[a["name"]] = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form":
            self._in = False


def form_of(html: str) -> _Form:
    f = _Form()
    f.feed(html)
    assert f.action, html[:2000]
    return f


def docker(*args: str, stdin: str | None = None) -> str:
    out = subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def moodle_script(*args: str) -> dict:
    docker("cp", str(HERE / "moodle_lti_cmi5.php"), f"{CONTAINER}:/tmp/moodle_lti_cmi5.php")
    return json.loads(docker("exec", CONTAINER, "php", "/tmp/moodle_lti_cmi5.php", *args).splitlines()[-1])


def _key_pair(pem: str) -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization

    private = serialization.load_pem_private_key(pem.encode(), password=None)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return pem, public


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """TrueNorth: a SQLite database with C105 accepted in a published course, this Moodle
    registered as the tenant's LTI platform (values its bootstrap wrote), and the test key
    as the LTI tool key."""
    sys.path.insert(0, str(HERE.parent / "api"))
    from _cmi5_kit import BUNDLE, CATALOGUE, CROSSWALK
    from app import programme_ingest, qsp_ingest
    from app.course_releases import service
    from app.db import Base
    from app.models import Course, ExternalPlatform, IntegrationAuthType, LTIToolKey, Tenant
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('db') / 'tn.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT, name="t", slug=f"t-{uuid.uuid4().hex[:6]}"))
    db.commit()
    qsp_ingest.import_crosswalk(db, CROSSWALK.read_text(encoding="utf-8"))
    programme_ingest.import_programme(db, CATALOGUE.read_text(encoding="utf-8"), tenant_id=TENANT)
    rel, _ = service.create_candidate(db, BUNDLE.read_bytes(), tenant_id=TENANT, user_id=None)
    service.accept(db, rel, user_id=None, acknowledge=[a["id"] for a in service.open_actions(rel)])
    db.get(Course, rel.course_id).is_published = True

    private, public = _key_pair(pathlib.Path(KEY).read_text())
    db.query(LTIToolKey).update({"is_active": False})
    db.add(LTIToolKey(kid="test-key", private_key_pem=private, public_key_pem=public, is_active=True))
    reg = json.loads(docker("exec", CONTAINER, "cat", "/var/www/moodledata/truenorth-registration.json"))
    assert reg["lti_issuer"].rstrip("/") == URL, reg
    platform = ExternalPlatform(
        name="Moodle test", slug=f"moodle-{uuid.uuid4().hex[:6]}", platform_type="moodle", base_url=URL,
        auth_type=IntegrationAuthType.lti13, tenant_id=TENANT, lti_issuer=reg["lti_issuer"],
        lti_client_id=reg["lti_client_id"], lti_deployment_id=reg["lti_deployment_id"],
        lti_auth_login_url=reg["lti_auth_login_url"], lti_token_url=reg["lti_token_url"],
        lti_jwks_url=reg["lti_jwks_url"],
    )
    db.add(platform)
    db.commit()
    return SimpleNamespace(db=db, release=rel, platform=platform)


@pytest.fixture
def api(world, monkeypatch):
    """TrueNorth's API on the world's database, over https (its LTI cookies are Secure), with
    real bearer-token checks, the in-memory LRS, and delivery on the world's session."""
    from _cmi5_kit import MemoryLRS
    from app import auth
    from app.cmi5 import ags, lms
    from app.db import get_db
    from app.main import app
    from fastapi.testclient import TestClient

    class Borrowed:
        def __getattr__(self, name):
            return getattr(world.db, name)

        def close(self):
            pass

    monkeypatch.setenv("CMI5_LRS_AUTH", "YXUta2V5OmF1LXNlY3JldA==")
    monkeypatch.setenv("LRS_AUTH", "c2VydmVyOnNlY3JldA==")
    monkeypatch.setattr(lms, "get_lms_backend", lambda m=MemoryLRS(): m)
    monkeypatch.setattr(ags, "open_session", lambda: Borrowed())
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    # AGS goes through app.net_guard, which never calls loopback; this disposable Moodle is
    # published on loopback only, so allow exactly that here (every other rule stays).
    from app import net_guard

    vetted = net_guard.refused
    monkeypatch.setattr(
        net_guard, "refused", lambda addr, *, allow_private: not addr.is_loopback and vetted(addr, allow_private=allow_private)
    )
    # The guard pins the first address a name resolves to; "localhost" may be ::1 first,
    # and the compose file publishes on 127.0.0.1 only.
    monkeypatch.setattr(
        net_guard, "_resolve", lambda host, port, **kw: socket.getaddrinfo(host, port, socket.AF_INET, **kw)
    )
    app.dependency_overrides[get_db] = lambda: world.db
    try:
        yield TestClient(app, base_url="https://testserver")
    finally:
        app.dependency_overrides.pop(get_db, None)


def _path(url: str) -> str:
    """A TrueNorth URL as Moodle wrote it (TN_TOOL_URL), as a path on the in-process API."""
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def moodle_login(username: str, password: str) -> httpx.Client:
    browser = httpx.Client(base_url=URL, follow_redirects=True, timeout=60)
    page = browser.get("/login/index.php").text
    token = re.search(r'name="logintoken" value="([^"]+)"', page).group(1)
    r = browser.post("/login/index.php", data={"username": username, "password": password, "logintoken": token})
    assert "MoodleSession" in browser.cookies and "loginerrormessage" not in r.text, r.text[:2000]
    return browser


def lti_launch(api, browser: httpx.Client, cmid: int) -> dict:
    """Open the activity in Moodle and carry each form to where it posts, as a browser does."""
    login = form_of(browser.get(f"/mod/lti/launch.php?id={cmid}").text)
    assert login.action.endswith("/lti/login"), login.action
    r = api.post(_path(login.action), data=login.fields, follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"].startswith(f"{URL}/mod/lti/auth.php"), r.headers["location"]
    launch = form_of(browser.get(r.headers["location"]).text)  # Moodle signs the id_token
    assert launch.action.endswith("/lti/launch") and "id_token" in launch.fields, launch.fields.keys()
    r = api.post(_path(launch.action), data=launch.fields, follow_redirects=False)
    assert r.status_code == 302, r.text
    code = r.headers["location"].split("/lti/session#code=", 1)[1]
    r = api.post("/lti/session", json={"code": code})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_moodle_student_launches_a_cmi5_au_and_the_grade_lands_in_moodle(world, api):
    from _cmi5_kit import AU, quiz_answers
    from app.cmi5.ags import deliver_due
    from app.cmi5.models import AGS_SENT, Cmi5AgsScore, Cmi5Registration
    from app.enrollment import ensure_enrollment
    from app.models import LTILaunch, User, UserRole

    rel = world.release
    username = f"cmi5lti{uuid.uuid4().hex[:8]}"
    m = moodle_script("--setup", f"--resource=cmi5:{rel.id}:0", f"--username={username}")

    # The Student TrueNorth knows for this Moodle account (as an earlier launch created
    # them), enrolled in the course: TrueNorth's enrolment, not Moodle's, admits them.
    student = User(id=uuid.uuid4(), keycloak_id=f"lti:{world.platform.id}:{m['userid']}", email=m["email"],
                   display_name="Test Student", role=UserRole.student, tenant_id=TENANT, source="lti")
    world.db.add(student)
    world.db.flush()
    ensure_enrollment(world.db, user_id=student.id, course_id=rel.course_id, tenant_id=TENANT)
    world.db.commit()

    # 1. The launch, through Moodle's own OIDC login and signed id_token.
    browser = moodle_login(m["username"], m["password"])
    session = lti_launch(api, browser, m["cmid"])
    assert session["target"] == f"/au/releases/{rel.id}?launch=0&lti=1"
    assert session["user"]["id"] == str(student.id)
    [launch] = world.db.query(LTILaunch).filter_by(user_id=student.id).all()
    assert (launch.resource_kind, launch.resource_id) == ("cmi5", f"{rel.id}:0")
    assert launch.ags_lineitem_url.startswith(f"{URL}/mod/lti/services.php/{m['course']}/lineitems/")
    assert world.db.query(Cmi5Registration).filter_by(user_id=student.id).count() == 1

    # 2. What the SPA does at that target, with the LTI session: launch, mark, report.
    bearer = {"Authorization": f"Bearer {session['access_token']}"}
    r = api.post(f"/cmi5/releases/{rel.id}/aus/0/launch", json={}, headers=bearer)
    assert r.status_code == 200, r.text
    au = AU(api, r.json()["url"]).start()
    assert au.initialized().status_code == 200
    key = quiz_answers(0)
    answers = {qid: (letter if i < 4 else ("A" if letter != "A" else "B")) for i, (qid, letter) in enumerate(key)}
    r = api.post(f"/cmi5/releases/{rel.id}/aus/0/grade", json={"answers": answers}, headers=bearer)
    assert r.status_code == 200 and r.json()["scaled"] == pytest.approx(0.8), r.text
    assert au.scored(0.8, marked=False).status_code == 200
    assert au.terminated().status_code == 200

    # 3. The mark went to Moodle's line item (after the statement's response), once.
    [row] = world.db.query(Cmi5AgsScore).filter_by(user_id=student.id).all()
    world.db.refresh(row)
    assert row.state == AGS_SENT, row.last_error
    assert asyncio.run(deliver_due(world.db)) == 0
    grade = moodle_script("--grade", f"--course={m['course']}", f"--instance={m['instance']}",
                          f"--userid={m['userid']}")
    assert grade == {"grademax": 100.0, "grade": 80.0}
