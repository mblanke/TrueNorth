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


class _Form:
    def __init__(self, action: str):
        self.action = action
        self.fields: dict[str, str] = {}


class _Forms(HTMLParser):
    """Every form of a page and what a browser submits with its default button: named inputs
    (checkboxes and radios only when checked; no other submit button), each select's
    selected option, textareas."""

    def __init__(self):
        super().__init__()
        self.forms: list[_Form] = []
        self._in = False
        self._select: str | None = None
        self._textarea: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.forms.append(_Form(a.get("action") or ""))
            self._in = True
        elif not self._in:
            return
        elif tag == "input" and a.get("name"):
            kind = a.get("type") or "text"
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            if kind == "submit" and a["name"] != "submitbutton":
                return  # e.g. "cancel": a browser sends only the button pressed
            self.forms[-1].fields[a["name"]] = a.get("value") or ""
        elif tag == "select" and a.get("name"):
            self._select = a["name"]
            self.forms[-1].fields.setdefault(self._select, "")
        elif tag == "option" and self._select and "selected" in a:
            self.forms[-1].fields[self._select] = a.get("value") or ""
        elif tag == "textarea" and a.get("name"):
            self._textarea = a["name"]
            self.forms[-1].fields[self._textarea] = ""

    def handle_data(self, data):
        if self._in and self._textarea:
            self.forms[-1].fields[self._textarea] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self._in = False
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None


def form_of(html: str, action_has: str = "") -> _Form:
    parser = _Forms()
    parser.feed(html)
    found = [f for f in parser.forms if action_has in f.action]
    assert found and found[0].action, html[:2000]
    return found[0]


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
    from app.models import Course, ExternalPlatform, LTIToolKey, Tenant
    from app.moodle_backends import install_cli
    from app.moodle_farm import service as farm
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('db') / 'tn.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    tenant_slug = f"t-{uuid.uuid4().hex[:6]}"
    db.add(Tenant(id=TENANT, name="t", slug=tenant_slug))
    db.commit()
    qsp_ingest.import_crosswalk(db, CROSSWALK.read_text(encoding="utf-8"))
    programme_ingest.import_programme(db, CATALOGUE.read_text(encoding="utf-8"), tenant_id=TENANT)
    rel, _ = service.create_candidate(db, BUNDLE.read_bytes(), tenant_id=TENANT, user_id=None)
    service.accept(db, rel, user_id=None, acknowledge=[a["id"] for a in service.open_actions(rel)])
    db.get(Course, rel.course_id).is_published = True

    private, public = _key_pair(pathlib.Path(KEY).read_text())
    db.query(LTIToolKey).update({"is_active": False})
    db.add(LTIToolKey(kid="test-key", private_key_pem=private, public_key_pem=public, is_active=True))
    db.commit()
    # Registered the way the installer registers the Moodle it runs (a farm node).
    reg = json.loads(docker("exec", CONTAINER, "cat", "/var/www/moodledata/truenorth-registration.json"))
    assert reg["lti_issuer"].rstrip("/") == URL, reg
    out = install_cli.register(db, tenant_slug, f"it{uuid.uuid4().hex[:6]}", URL, reg)
    platform = db.get(ExternalPlatform, uuid.UUID(out["platform_id"]))
    assert farm.is_managed(db, platform)
    return SimpleNamespace(db=db, release=rel, platform=platform, pem=private)


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


def lti_launch_raw(api, browser: httpx.Client, cmid: int) -> tuple[httpx.Response, dict]:
    """Open the activity in Moodle and carry each form to where it posts, as a browser does.
    Returns TrueNorth's answer to the launch and the claims Moodle signed (read, not trusted)."""
    import jwt

    login = form_of(browser.get(f"/mod/lti/launch.php?id={cmid}").text)
    assert login.action.endswith("/lti/login"), login.action
    r = api.post(_path(login.action), data=login.fields, follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"].startswith(f"{URL}/mod/lti/auth.php"), r.headers["location"]
    launch = form_of(browser.get(r.headers["location"]).text)  # Moodle signs the id_token
    assert launch.action.endswith("/lti/launch") and "id_token" in launch.fields, launch.fields.keys()
    claims = jwt.decode(launch.fields["id_token"], options={"verify_signature": False})
    r = api.post(_path(launch.action), data=launch.fields, follow_redirects=False)
    assert r.status_code == 302, r.text
    return r, claims


def lti_launch(api, browser: httpx.Client, cmid: int) -> dict:
    """A launch by an account the LTI launch created: the hand-off session it is given."""
    r, _ = lti_launch_raw(api, browser, cmid)
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


def sso_browser(world, user, course_idnumber: str) -> httpx.Client:
    """A browser signed in to Moodle the way TrueNorth's "Open in Moodle" does it (app.moodle_sso):
    a one-minute ticket POSTed to local_truenorth's sso.php, which creates the farm account
    (username tn-<id>, idnumber the TrueNorth id, locked) and enrols it on the course."""
    import time

    import jwt

    now = int(time.time())
    claims = {
        "iss": "truenorth", "typ": "sso", "aud": URL, "tid": str(TENANT), "sub": str(user.id), "email": user.email,
        "given_name": "Farm", "family_name": "Student", "role": "student", "course": course_idnumber,
        "iat": now, "exp": now + 60, "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(claims, world.pem, algorithm="RS256", headers={"kid": "test-key"})
    browser = httpx.Client(base_url=URL, follow_redirects=True, timeout=60)
    r = browser.post("/local/truenorth/sso.php", data={"token": token}, follow_redirects=False)
    assert r.status_code in (302, 303), r.text[:500]
    assert "MoodleSession" in browser.cookies
    return browser


def test_a_farm_students_account_from_truenorth_sign_in_launches_and_gets_the_grade(world, api):
    """The farm's main case: the Student opens Moodle from TrueNorth (SSO), then the cmi5
    activity. Their Moodle account reaches their TrueNorth account by its locked TrueNorth id
    and tn-<id> username (app/moodle_farm), not by email, so the grade goes back."""
    from _cmi5_kit import AU
    from app.auth import CurrentUser, get_current_user
    from app.cmi5.models import AGS_SENT, Cmi5AgsScore
    from app.enrollment import ensure_enrollment
    from app.lti_identity.models import LTIUserLink
    from app.main import app
    from app.models import LTILaunch, User, UserRole

    rel = world.release
    course_idnumber = f"tn-it-{uuid.uuid4().hex[:8]}"
    m = moodle_script("--setup", f"--resource=cmi5:{rel.id}:1", f"--course-idnumber={course_idnumber}")
    # A TrueNorth Student with their own TrueNorth sign-in, enrolled in TrueNorth.
    student = User(id=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email=f"farm-{uuid.uuid4().hex[:8]}@x.test",
                   display_name="Farm Student", role=UserRole.student, tenant_id=TENANT)
    world.db.add(student)
    world.db.flush()
    ensure_enrollment(world.db, user_id=student.id, course_id=rel.course_id, tenant_id=TENANT)
    world.db.commit()

    browser = sso_browser(world, student, course_idnumber)
    who = moodle_script("--whois", f"--idnumber={student.id}")
    assert who["username"] == f"tn-{student.id}" and who["idnumber_lock"] == "locked"

    # The Student cannot change the id their grades are bound by: Moodle's own profile form,
    # submitted with another idnumber and a new city, takes the city and keeps the idnumber.
    edit = form_of(browser.get(f"/user/edit.php?id={who['userid']}").text, "user/edit.php")
    fields = {**edit.fields, "idnumber": str(uuid.uuid4()), "city": "Spoofville"}
    browser.post(_path(edit.action) if edit.action.startswith("http") else edit.action, data=fields)
    after = moodle_script("--whois", f"--idnumber={student.id}")
    assert after["city"] == "Spoofville", "the profile form was not accepted; the lock was not exercised"
    assert after["idnumber"] == str(student.id) and after["userid"] == who["userid"]

    # The launch: Moodle's claims for a farm account, as TrueNorth binds them.
    resp, claims = lti_launch_raw(api, browser, m["cmid"])
    assert claims["sub"] == str(who["userid"])
    assert claims["https://purl.imsglobal.org/spec/lti/claim/lis"]["person_sourcedid"] == str(student.id)
    assert claims["https://purl.imsglobal.org/spec/lti/claim/ext"]["user_username"] == f"tn-{student.id}"
    assert claims["email"] == student.email  # also matches, but is not what binds it
    assert resp.headers["location"].endswith(f"/au/releases/{rel.id}?launch=1&lti=1")  # no hand-off: own sign-in
    [link] = world.db.query(LTIUserLink).filter_by(platform_id=world.platform.id, user_id=student.id).all()
    assert link.lti_sub == str(who["userid"])
    [launch] = world.db.query(LTILaunch).filter_by(user_id=student.id).all()
    assert launch.ags_lineitem_url.startswith(f"{URL}/mod/lti/services.php/{m['course']}/lineitems/")

    # The Student, signed in to TrueNorth, launches and passes the module (TrueNorth marks it).
    who_tn = CurrentUser(id=str(student.id), email=student.email, display_name=student.display_name,
                         role=student.role, tenant_id=str(TENANT), keycloak_id=student.keycloak_id)
    app.dependency_overrides[get_current_user] = lambda: who_tn
    try:
        r = api.post(f"/cmi5/releases/{rel.id}/aus/1/launch", json={})
        assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    au = AU(api, r.json()["url"], student).start()
    assert au.initialized().status_code == 200
    assert au.scored(0.8).status_code == 200
    [row] = world.db.query(Cmi5AgsScore).filter_by(user_id=student.id).all()
    world.db.refresh(row)
    assert row.state == AGS_SENT, row.last_error
    grade = moodle_script("--grade", f"--course={m['course']}", f"--instance={m['instance']}",
                          f"--userid={who['userid']}")
    assert grade == {"grademax": 100.0, "grade": 80.0}
