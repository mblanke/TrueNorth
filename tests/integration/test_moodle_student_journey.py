"""M1: the complete Moodle Student loop, in a real browser (Chromium, Playwright).

Runs in the CI ``moodle`` lane against the disposable Moodle of compose.moodle-test.yml
(its LRS included) and TrueNorth served by this test: the production SPA build and the
API on one origin (tests/integration/_journey_server.py, as nginx lays them out), on its
own SQLite file, in its own process. Skipped unless configured:

    MOODLE_TEST_URL, MOODLE_TEST_KEY, MOODLE_TEST_CONTAINER   as test_moodle_publish.py
    MOODLE_JOURNEY_SPA     the SPA build's ``browser`` directory (npm run build:prod);
                           default control-plane/web/dist/truenorth-range-web/browser
    MOODLE_JOURNEY_PORT    where TrueNorth listens (default 18095); Moodle must have been
                           started with TN_TOOL_URL=http://localhost:<port>/api
    MOODLE_TEST_LRS_URL    the compose file's LRS (default http://127.0.0.1:18094)
    MOODLE_JOURNEY_REQUIRED=1   a missing SPA build, Playwright or LRS fails instead of skipping

What is real: Chromium; the SPA; the API with real bearer-token checks (generic OIDC
against a key set this test serves); Moodle 5.2.3 with local_truenorth (sign-in, quiz,
gradebook, completion, LTI 1.3, the signed results pull); the LRS (lrsql). What is not:
Keycloak's own pages. The browser's three Keycloak calls (check-sso, its cookie probe, the
code exchange) are answered here, as the e2e lane does (tests/e2e/support/identity.ts),
but with tokens signed by this test's identity provider, which the API verifies.

The journey (one Student, one course: the C105 release published to Moodle):
 1. The Student signs in to TrueNorth, opens the course and clicks "Open in Moodle"; the
    SSO ticket signs them into Moodle in a new tab, on the course.
 2. In Moodle they open a page, start the module 1 quiz, answer one question and leave,
    come back ("Continue your attempt"), answer the rest and submit.
 3. Moodle is stopped and a pull is triggered: it fails, and the course page in TrueNorth
    says the Moodle results may be out of date, without the grade. Moodle is started, the
    next pull brings the grade and completion: shown on the Student's course page with
    Moodle's time and "synced", recorded once on the right enrolment with Moodle's attempt
    id and the release; pulling again from the start adds nothing.
 4. From the Moodle course the Student opens a cmi5 module (an External tool activity, as
    a teacher deep-links it): Moodle's LTI launch reaches TrueNorth, which launches the AU
    in the SPA; its statements reach the LRS naming the Student by account (TrueNorth id,
    no email), with the registration and activity ids TrueNorth issued.
Refused, each in a browser: a Student not enrolled ("Open in Moodle" says so, no ticket);
a reused ticket; an expired ticket; a ticket for another tenant (a person of that tenant
with the same mailbox as the Student).
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

URL = os.getenv("MOODLE_TEST_URL", "").rstrip("/")
TENANT = uuid.UUID(os.getenv("MOODLE_TEST_TENANT", "7e57e57e-0000-4000-8000-000000000001"))
KEY = os.getenv("MOODLE_TEST_KEY", "")
CONTAINER = os.getenv("MOODLE_TEST_CONTAINER", "tn-moodle-test-moodle-1")
HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPA = pathlib.Path(os.getenv("MOODLE_JOURNEY_SPA") or ROOT / "control-plane/web/dist/truenorth-range-web/browser")
PORT = int(os.getenv("MOODLE_JOURNEY_PORT", "18095"))
TN = f"http://localhost:{PORT}"
LRS = os.getenv("MOODLE_TEST_LRS_URL", "http://127.0.0.1:18094").rstrip("/")
LRS_AUTH = base64.b64encode(b"truenorth-lrs-key:truenorth-lrs-secret").decode()  # compose.moodle-test.yml
REQUIRED = os.getenv("MOODLE_JOURNEY_REQUIRED") == "1"
ISSUER = f"{TN}/auth/realms/truenorth"
CLIENT = "truenorth-web"
REFUSED = "TrueNorth sign-in was refused"  # local_truenorth ssodenied

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (URL and KEY), reason="MOODLE_TEST_URL and MOODLE_TEST_KEY are not set"),
]


def _missing(reason: str):
    if REQUIRED:
        pytest.fail(f"MOODLE_JOURNEY_REQUIRED=1 but {reason}")
    pytest.skip(reason)


# -- the test's identity provider -------------------------------------------------------
class Idp:
    """Signs the tokens the browser gets for its Keycloak code exchange; the API verifies
    them against ``/test-idp/jwks`` (AUTH_BACKEND=generic_oidc)."""

    def __init__(self, tmp: pathlib.Path):
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        self.jwks_file = tmp / "jwks.json"
        self.jwks_file.write_text(json.dumps({"keys": [{**jwk, "kid": "journey-idp", "alg": "RS256", "use": "sig"}]}))

    def token(self, sub: str, typ: str = "Bearer", email: str = "") -> str:
        import jwt

        now = int(time.time())
        # amr: signed in with a password and a one-time code, as Keycloak's tn-browser flow
        # reports it for staff (app/mfa_policy.py). Students are not asked; it is harmless there.
        claims = {"iss": ISSUER, "aud": CLIENT, "azp": CLIENT, "sub": sub, "typ": typ, "iat": now, "exp": now + 3600,
                  "preferred_username": sub, "email": email, "amr": ["pwd", "otp"]}
        return jwt.encode(claims, self.pem, algorithm="RS256", headers={"kid": "journey-idp"})


def signed_in_context(browser, idp: Idp, person):
    """A browser profile in which Keycloak says ``person`` is signed in."""
    ctx = browser.new_context(base_url=TN)

    def answer(route):
        url = urlsplit(route.request.url)
        endpoint = url.path.split("/protocol/openid-connect/", 1)[1]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        html = "<!doctype html><html><body>{}</body></html>"
        if endpoint.startswith("3p-cookies/"):
            return route.fulfill(status=200, content_type="text/html",
                                 body=html.format('<script>parent.postMessage("supported", "*");</script>'))
        if endpoint == "auth":  # the silent check-sso iframe: back with a code, as Keycloak would
            target = json.dumps(f"{q.get('redirect_uri', '')}#state={q.get('state', '')}&session_state=j&code=j-code")
            return route.fulfill(status=200, content_type="text/html",
                                 body=html.format(f"<script>location.replace({target});</script>"))
        if endpoint == "token":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "access_token": idp.token(person.keycloak_id, email=person.email),
                "refresh_token": idp.token(person.keycloak_id, "Refresh"),
                "token_type": "Bearer", "expires_in": 3600, "refresh_expires_in": 3600, "session_state": "j"}))
        return route.fulfill(status=400, content_type="application/json", body='{"error":"invalid_request"}')

    ctx.route(re.compile(r"/realms/[^/]+/protocol/openid-connect/"), answer)
    return ctx


# -- helpers ------------------------------------------------------------------------------
def docker(*args: str) -> str:
    out = subprocess.run(["docker", *args], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def moodle_script(*args: str) -> dict:
    docker("cp", str(HERE / "moodle_lti_cmi5.php"), f"{CONTAINER}:/tmp/moodle_lti_cmi5.php")
    return json.loads(docker("exec", CONTAINER, "php", "/tmp/moodle_lti_cmi5.php", *args).splitlines()[-1])


def wait_http(url: str, seconds: float, ok=lambda r: r.status_code < 500) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if ok(httpx.get(url, timeout=5)):
                return
        except httpx.HTTPError:
            pass
        assert time.monotonic() < deadline, f"{url} did not answer within {seconds:.0f}s"
        time.sleep(2)


def ticket(world, sub: uuid.UUID, tenant: uuid.UUID, *, age: int = 0, course: str | None = None) -> str:
    """A sign-in ticket as app.moodle_sso mints it, signed with the tool key. ``age`` moves
    its issue time into the past (an expired ticket); ``tenant`` may be another tenant's."""
    import jwt

    now = int(time.time()) - age
    claims = {"iss": "truenorth", "typ": "sso", "aud": URL, "tid": str(tenant), "sub": str(sub),
              "email": "x@example.test", "given_name": "X", "family_name": "Y", "role": "student",
              "iat": now, "exp": now + 60, "jti": uuid.uuid4().hex}
    if course:
        claims["course"] = course
    return jwt.encode(claims, world.pem, algorithm="RS256", headers={"kid": "test-key"})


def post_ticket(page, token: str):
    """POST a ticket to sso.php from the browser, as the SPA's hidden form does."""
    page.set_content(f'<form method="post" action="{URL}/local/truenorth/sso.php">'
                     f'<input type="hidden" name="token" value="{token}"></form>')
    with page.expect_navigation():
        page.evaluate("document.forms[0].submit()")
    return page


def _norm(text: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", text or "").split()).strip().lower()


def answer_page(page, questions: list[dict]) -> int:
    """Answer the question on this quiz page correctly: find it by its text among the
    release's questions, tick the options whose text is a right answer. Returns how many
    questions were answered."""
    answered = 0
    for q in page.locator(".que").all():
        stem = _norm(q.locator(".qtext").inner_text())
        match = next((x for x in questions if _norm(x["stem"]) == stem), None)
        assert match, f"question not in the release: {stem[:80]}"
        right = {_norm(match["options"][i]) for i in match["correct"]}
        for box in q.locator("input[type=radio], input[type=checkbox]").all():
            label_id = box.get_attribute("aria-labelledby")
            if not label_id:
                continue
            label = page.locator(f"[id='{label_id}']").inner_text()
            text = _norm(re.sub(r"^\s*[a-z]\.\s*", "", label, flags=re.I))
            if text in right:
                box.check()
        answered += 1
    return answered


# -- the world ------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    if not SPA.joinpath("index.html").is_file():
        _missing(f"no SPA build at {SPA} (cd control-plane/web && npm run build:prod)")
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        _missing("Playwright is not installed (requirements-test.txt; python -m playwright install chromium)")
    try:
        httpx.get(f"{LRS}/health", timeout=5).raise_for_status()
    except httpx.HTTPError:
        _missing(f"no LRS at {LRS} (compose.moodle-test.yml service lrs)")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            pytest.fail(f"port {PORT} is taken; set MOODLE_JOURNEY_PORT (and Moodle's TN_TOOL_URL to match)")
    reg = json.loads(docker("exec", CONTAINER, "cat", "/var/www/moodledata/truenorth-registration.json"))
    assert reg["lti_issuer"].rstrip("/") == URL, reg

    sys.path.insert(0, str(ROOT / "tests/api"))
    from _cmi5_kit import BUNDLE, CATALOGUE, CROSSWALK
    from app import programme_ingest, qsp_ingest
    from app.course_publishing import service as publishing
    from app.course_publishing.payload import _modules
    from app.course_releases import service as releases
    from app.course_releases.service import load_bundle
    from app.db import Base
    from app.enrollment import ensure_enrollment
    from app.lms.lrsql_admin import mint_au_credential
    from app.models import Course, ExternalPlatform, LTIToolKey, Tenant, User, UserRole
    from app.moodle_backends import LocalTrueNorthMoodle, install_cli
    from cryptography.hazmat.primitives import serialization
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    tmp = tmp_path_factory.mktemp("journey")
    db_file = tmp / "tn.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    db = session()

    slug = f"journey-{uuid.uuid4().hex[:6]}"
    db.add(Tenant(id=TENANT, name="Journey unit", slug=slug))
    other = Tenant(id=uuid.uuid4(), name="Other unit", slug=f"other-{uuid.uuid4().hex[:6]}")
    db.add(other)
    db.commit()
    qsp_ingest.import_crosswalk(db, CROSSWALK.read_text(encoding="utf-8"))
    programme_ingest.import_programme(db, CATALOGUE.read_text(encoding="utf-8"), tenant_id=TENANT)

    # TrueNorth's LTI tool key is the test key, whose public half the Moodle trusts.
    pem = pathlib.Path(KEY).read_text()
    public = serialization.load_pem_private_key(pem.encode(), password=None).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    db.add(LTIToolKey(kid="test-key", private_key_pem=pem, public_key_pem=public, is_active=True))
    db.commit()

    # The Moodle, registered as the installer registers a farm node.
    out = install_cli.register(db, slug, f"j{uuid.uuid4().hex[:6]}", URL, reg)
    platform = db.get(ExternalPlatform, uuid.UUID(out["platform_id"]))

    # C105: accepted, published in TrueNorth and to the Moodle (staged, verified, live).
    rel, _ = releases.create_candidate(db, BUNDLE.read_bytes(), tenant_id=TENANT, user_id=None)
    releases.accept(db, rel, user_id=None, acknowledge=[a["id"] for a in releases.open_actions(rel)])
    db.get(Course, rel.course_id).is_published = True
    db.commit()
    backend = LocalTrueNorthMoodle(key_provider=lambda: (pem, "test-key"))
    pub, _ = publishing.request(db, rel, platform, user_id=None)
    db.commit()
    pub = publishing.run(db, pub, backend=backend)
    assert pub.state == "published", pub.error
    described = backend.describe_course(platform, str(rel.course_id))
    modules = _modules(load_bundle(db, rel))

    def person(role: UserRole, tenant: uuid.UUID, email: str | None = None, name: str = "Journey") -> User:
        uid = uuid.uuid4()
        u = User(id=uid, keycloak_id=f"kc-{uid}", email=email or f"journey-{uid.hex[:8]}@example.test",
                 display_name=f"{name} {role.value}", first_name=name, last_name=role.value.title(),
                 role=role, tenant_id=tenant, onboarding_state="complete", onboarded_at=datetime.now(UTC))
        db.add(u)
        db.flush()
        return u

    student = person(UserRole.student, TENANT, name="Jo")
    outsider = person(UserRole.student, TENANT, name="Out")
    admin = person(UserRole.admin, TENANT, name="Ada")
    twin = person(UserRole.student, other.id, email=student.email.upper(), name="Twin")  # same mailbox
    # Assigned by staff (as an approval or an admin would), not a self-enrolment.
    enrollment = ensure_enrollment(db, user_id=student.id, course_id=rel.course_id, tenant_id=TENANT, source="assigned")
    db.commit()

    # A teacher's deep-linked cmi5 module in the same Moodle course (the farm image's tool).
    cmi5 = moodle_script("--setup", f"--resource=cmi5:{rel.id}:0", f"--into={rel.course_id}")

    idp = Idp(tmp)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT / "control-plane/api"), str(HERE)]),
        "DATABASE_URL": f"sqlite:///{db_file}",
        "DB_AUTO_CREATE": "false",
        "SEED_DEV_DATA": "false",
        "AUTH_DISABLED": "false",
        "AUTH_BACKEND": "generic_oidc",
        "OIDC_JWKS_URL": f"http://127.0.0.1:{PORT}/test-idp/jwks",
        "OIDC_ISSUER": ISSUER,
        "OIDC_AUDIENCE": CLIENT,
        "LTI_TOOL_BASE_URL": f"{TN}/api",
        "LTI_WEB_BASE_URL": TN,
        "CORS_ORIGINS": TN,
        "RATE_LIMIT_ENABLED": "false",
        "REDIS_URL": "",
        "PLATFORM_TENANT_ID": str(TENANT),  # two tenants exist here
        # A scheduled pull exists but does not fire during the test: every pull here is triggered.
        "MOODLE_RESULTS_PULL_SECONDS": "3600",
        "LAB_SESSIONS_SWEEP": "false",
        "EXERCISE_CLOCK": "false",
        "SCHEDULER_CLOCK_ENABLED": "false",
        "WS_HEARTBEAT": "false",
        "COURSE_PUBLISH_RESUME": "false",
        "LRS_URL": LRS,
        "LRS_AUTH": LRS_AUTH,
        "CMI5_LRS_AUTH": mint_au_credential(LRS, "lrsadmin", "lrsadmin"),
        "JOURNEY_SPA_DIST": str(SPA),
        "JOURNEY_IDP_JWKS": str(idp.jwks_file),
    }
    log = (tmp / "server.log").open("w")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "_journey_server:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=HERE, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        wait_http(f"http://127.0.0.1:{PORT}/api/health/live", 90, ok=lambda r: r.status_code == 200)
        yield SimpleNamespace(
            db=db, Session=session, tenant=TENANT, other=other, platform=platform, release=rel, course_id=rel.course_id,
            student=student, outsider=outsider, admin=admin, twin=twin, enrollment=enrollment, pem=pem,
            backend=backend, described=described, modules=modules, cmi5=cmi5, idp=idp, tmp=tmp,
            server_log=tmp / "server.log",
        )
    finally:
        server.terminate()
        try:
            server.wait(20)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        if artifacts := os.getenv("MOODLE_JOURNEY_ARTIFACTS"):
            pathlib.Path(artifacts).mkdir(parents=True, exist_ok=True)
            pathlib.Path(artifacts, "truenorth-server.log").write_text((tmp / "server.log").read_text())


class Api:
    """TrueNorth's API as ``person``, with a bearer token from the test's identity provider
    and the CSRF double-submit the SPA does."""

    def __init__(self, world, person):
        token = world.idp.token(person.keycloak_id, email=person.email)
        self.c = httpx.Client(base_url=f"http://127.0.0.1:{PORT}/api", timeout=240,
                              headers={"Authorization": f"Bearer {token}"})
        self.c.get("/health/live")  # sets the CSRF cookie

    def get(self, path: str, **kw) -> httpx.Response:
        return self.c.get(path, **kw)

    def post(self, path: str, **kw) -> httpx.Response:
        return self.c.post(path, headers={"X-CSRF-Token": self.c.cookies.get("truenorth_csrf", "")}, **kw)


def pull(world, **body) -> httpx.Response:
    """The explicit trigger: an integration admin's "Pull now"."""
    return Api(world, world.admin).post(f"/integrations/platforms/{world.platform.id}/moodle-results/pull", json=body)


def results(world) -> dict:
    """The Student's own view of their Moodle results on the course (what the SPA shows)."""
    r = Api(world, world.student).get("/integrations/moodle/results", params={"course_id": str(world.course_id)})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def browser(world):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def traced(browser, world, request):
    """New browser profiles for this test. With MOODLE_JOURNEY_ARTIFACTS set (CI), each one's
    Playwright trace (screenshots, DOM, network) is written there."""
    made = []
    artifacts = os.getenv("MOODLE_JOURNEY_ARTIFACTS")

    def make(person=None):
        ctx = signed_in_context(browser, world.idp, person) if person else browser.new_context(base_url=TN)
        ctx.set_default_timeout(60_000)
        if artifacts:
            ctx.tracing.start(screenshots=True, snapshots=True)
        made.append(ctx)
        return ctx

    yield make
    for n, ctx in enumerate(made):
        if artifacts:
            pathlib.Path(artifacts).mkdir(parents=True, exist_ok=True)
            ctx.tracing.stop(path=str(pathlib.Path(artifacts) / f"{request.node.name}-{n}.zip"))
        ctx.close()


def open_in_moodle(ctx, world, tickets: list | None = None):
    """Sign in to TrueNorth, open the course, click "Open in Moodle": returns the TrueNorth
    page and the Moodle tab, which the SSO ticket signed in."""
    if tickets is not None:
        ctx.on("request", lambda r: tickets.append(r.post_data)
               if r.method == "POST" and "/local/truenorth/sso.php" in r.url else None)
    page = ctx.new_page()
    page.goto(f"/learning/courses/{world.course_id}")
    button = page.get_by_role("button", name=re.compile("Open in Moodle"))
    button.wait_for()
    with ctx.expect_page() as tab:
        button.click()
    moodle = tab.value
    moodle.wait_for_url(re.compile(r"/course/view\.php\?id=\d+"))
    return page, moodle


def follow(moodle, href_part: str, fallback: str) -> None:
    """Click the activity's link on the course page; if its section is folded away, open the
    address the link points at."""
    link = moodle.locator(f'a[href*="{href_part}"]').first
    if link.count() and link.is_visible():
        link.click()
    else:
        moodle.goto(fallback)


def module_one(world) -> tuple[int, int, str, list[dict]]:
    acts = world.described["activities"]
    page_cm = acts["tn:mod_001:page:01"]["cmid"]
    quiz_idnumber = next(k for k, a in acts.items() if k.startswith("tn:mod_001:quiz:") and a["visible"])
    questions = next(m for m in world.modules if int(m["ordinal"]) == 1)["questions"]
    return page_cm, acts[quiz_idnumber]["cmid"], quiz_idnumber, questions


# -- the journey ---------------------------------------------------------------------------
def test_the_student_loop_from_truenorth_through_moodle_and_back(world, traced):
    from app.models import ModuleProgress, ModuleProgressStatus, QuizAttempt
    from app.moodle_results.models import MoodleResultRecord

    assert pull(world).status_code == 200  # before anything: current, nothing of theirs yet
    assert results(world)["sync"][0]["state"] == "current"

    # 1. TrueNorth, then Moodle through the SSO ticket.
    ctx = traced(world.student)
    tickets: list = []
    page, moodle = open_in_moodle(ctx, world, tickets)
    assert len(tickets) == 1 and tickets[0].startswith("token="), tickets
    world.used_ticket = parse_qs(tickets[0])["token"][0]
    course_url = moodle.url
    page_cm, quiz_cm, quiz_idnumber, questions = module_one(world)

    # 2. A page, then the quiz: start, answer one, leave, resume, finish.
    follow(moodle, f"/mod/page/view.php?id={page_cm}", f"{URL}/mod/page/view.php?id={page_cm}")
    moodle.wait_for_url(re.compile(rf"/mod/page/view\.php\?id={page_cm}"))
    moodle.goto(course_url)
    follow(moodle, f"/mod/quiz/view.php?id={quiz_cm}", f"{URL}/mod/quiz/view.php?id={quiz_cm}")
    moodle.get_by_role("button", name=re.compile(r"Attempt quiz", re.I)).click()
    moodle.wait_for_url(re.compile(r"/mod/quiz/attempt\.php"))
    attempt_id = int(parse_qs(urlsplit(moodle.url).query)["attempt"][0])
    assert answer_page(moodle, questions) == 1
    moodle.get_by_role("button", name=re.compile(r"^Next page")).click()  # saves the answer
    moodle.wait_for_url(re.compile(r"page=1"))
    moodle.goto(course_url)  # leaves mid-attempt

    follow(moodle, f"/mod/quiz/view.php?id={quiz_cm}", f"{URL}/mod/quiz/view.php?id={quiz_cm}")
    moodle.get_by_role("button", name=re.compile(r"Continue (your|the last) attempt", re.I)).click()
    moodle.wait_for_url(re.compile(rf"/mod/quiz/attempt\.php\?attempt={attempt_id}"))
    answered = 1
    for _ in range(len(questions) + 2):
        if answered < len(questions):
            answered += answer_page(moodle, questions)
        finish = moodle.get_by_role("button", name=re.compile(r"^Finish attempt"))
        if finish.count():
            finish.click()
            break
        moodle.get_by_role("button", name=re.compile(r"^Next page")).click()
        moodle.wait_for_load_state()
    assert answered == len(questions)
    moodle.wait_for_url(re.compile(r"/mod/quiz/summary\.php"))
    moodle.get_by_role("button", name="Submit all and finish").click()
    moodle.get_by_role("dialog").get_by_role("button", name="Submit all and finish").click()
    moodle.wait_for_url(re.compile(r"/mod/quiz/review\.php"))
    moodle.wait_for_timeout(7000)  # local_truenorth leaves rows younger than its 5 s settle window for later

    # 3. A delayed result: Moodle is down when the pull runs.
    docker("stop", "-t", "10", CONTAINER)
    try:
        down = pull(world)
        assert down.status_code == 502, down.text
        page.reload()
        page.locator('[data-moodle-results] [data-sync-state="failing"]').wait_for()
        assert "may be out of date" in page.locator("[data-moodle-results]").inner_text()
        assert page.locator('[data-moodle-results] [data-kind="quiz_grade"]').count() == 0
        delayed = results(world)
        assert delayed["sync"][0]["stale"] is True and delayed["sync"][0]["last_error"] == ""
        assert not [r for r in delayed["results"] if r["kind"] == "quiz_grade"]
    finally:
        docker("start", CONTAINER)
        wait_http(f"{URL}/login/index.php", 600, ok=lambda r: r.status_code == 200)

    # Recovery: the next pull reconciles; the Student sees grade, completion, Moodle's time, synced.
    up = pull(world)
    assert up.status_code == 200 and up.json()["applied"] >= 2, up.text
    page.reload()
    panel = page.locator("[data-moodle-results]")
    panel.locator('[data-sync-state="current"]').wait_for()
    grade_row = panel.locator(f'[data-kind="quiz_grade"][data-activity="{quiz_idnumber}"]')
    grade_row.wait_for()
    assert "Module 1 quiz: 100 / 100" in grade_row.inner_text(), grade_row.inner_text()
    assert "in Moodle" in grade_row.inner_text()
    assert ": complete" in panel.locator('[data-kind="completion"][data-activity="tn:mod_001:page:01"]').inner_text()

    mine = results(world)
    assert mine["enrollment_id"] == str(world.enrollment.id) and mine["sync"][0]["state"] == "current"
    grade = next(r for r in mine["results"] if r["kind"] == "quiz_grade" and r["activity"] == quiz_idnumber)
    assert (grade["grade"], grade["grade_max"]) == (100.0, 100.0)
    assert grade["source_attempt_id"] == attempt_id and grade["release_id"] == str(world.release.id)
    assert grade["source_time"] and grade["quiz_attempt_id"]

    # Pulling again from the start finds everything already recorded: nothing doubles.
    again = pull(world, reset=True)
    assert again.status_code == 200 and again.json()["applied"] == 0, again.text
    db = world.Session()
    try:
        records = db.query(MoodleResultRecord).filter_by(user_id=world.student.id, course_id=world.course_id).all()
        refs = [r.ref for r in records]
        assert len(refs) == len(set(refs)) and f"quiz_grade:{quiz_idnumber}" in refs
        mirrored = db.query(QuizAttempt).filter(QuizAttempt.user_id == world.student.id).all()
        assert len(mirrored) == 1 and mirrored[0].passed is True
        progress = db.query(ModuleProgress).filter_by(enrollment_id=world.enrollment.id).all()
        assert any(p.status == ModuleProgressStatus.completed and p.score == 100 for p in progress)
        # The other tenant's person with the same mailbox got nothing.
        assert db.query(MoodleResultRecord).filter_by(user_id=world.twin.id).count() == 0
    finally:
        db.close()


def test_a_cmi5_module_opened_in_moodle_reaches_the_lrs_as_the_student(world, traced):
    from app.cmi5.models import Cmi5Registration
    from app.lti_identity.models import LTIUserLink

    ctx = traced(world.student)
    open_in_moodle(ctx, world)
    # The External tool opens in a new window (Launch container: New window): Moodle's
    # launch page, which carries the LTI 1.3 login to TrueNorth.
    au = ctx.new_page()
    au.goto(f"{URL}/mod/lti/launch.php?id={world.cmi5['cmid']}")
    au.wait_for_url(re.compile(rf"/au/releases/{world.release.id}/0\?.*registration="), timeout=120_000)
    q = {k: v[0] for k, v in parse_qs(urlsplit(au.url).query).items()}
    actor, registration, activity = json.loads(q["actor"]), q["registration"], q["activityId"]

    def statements() -> list[dict]:
        r = httpx.get(f"{LRS}/xapi/statements", params={"registration": registration, "limit": 50},
                      headers={"Authorization": f"Basic {LRS_AUTH}", "X-Experience-API-Version": "1.0.3"},
                      timeout=20)
        r.raise_for_status()
        return r.json()["statements"]

    # Wait inside Playwright, not in time.sleep: the sync API answers the page's stubbed
    # Keycloak calls only while the test is in a Playwright call.
    deadline = time.monotonic() + 60
    while not {"launched", "initialized"} <= {s["verb"]["id"].rsplit("/", 1)[-1] for s in statements()}:
        assert time.monotonic() < deadline, [s["verb"]["id"] for s in statements()]
        au.wait_for_timeout(2000)
    stored = statements()
    for s in stored:
        assert s["actor"]["account"] == actor["account"] and "mbox" not in json.dumps(s["actor"])
        assert s["actor"]["account"]["name"] == str(world.student.id)
        assert world.student.email.lower() not in json.dumps(s).lower()
        assert s["context"]["registration"] == registration
    au_statements = [s for s in stored if s["verb"]["id"].endswith(("/launched", "/initialized"))]
    assert {s["object"]["id"] for s in au_statements} == {activity}

    db = world.Session()
    try:
        reg = db.query(Cmi5Registration).filter_by(user_id=world.student.id, release_id=world.release.id).one()
        assert str(reg.id) == registration
        # Moodle's account for the Student (made by TrueNorth's sign-in) is bound to them.
        assert db.query(LTIUserLink).filter_by(platform_id=world.platform.id, user_id=world.student.id).count() == 1
    finally:
        db.close()


# -- refusals --------------------------------------------------------------------------------
def test_a_student_not_enrolled_gets_no_ticket(world, traced):
    ctx = traced(world.outsider)
    sent: list = []
    ctx.on("request", lambda r: sent.append(r.url) if "/local/truenorth/sso.php" in r.url else None)
    page = ctx.new_page()
    page.goto(f"/learning/courses/{world.course_id}")
    page.get_by_role("button", name=re.compile("Open in Moodle")).click()
    page.locator(".moodle-err").wait_for()
    assert "not enrolled" in page.locator(".moodle-err").inner_text()
    assert sent == []


def _refused(page) -> None:
    assert "/local/truenorth/sso.php" in page.url, page.url  # no redirect into Moodle
    assert REFUSED in page.content()


def test_a_used_ticket_is_refused(world, traced):
    token = getattr(world, "used_ticket", None)
    if token is None:  # the journey did not get that far: spend one fresh ticket first
        token = ticket(world, world.student.id, TENANT, course=str(world.course_id))
        accepted = post_ticket(traced().new_page(), token)
        assert re.search(r"/course/view\.php\?id=\d+", accepted.url), accepted.url
    _refused(post_ticket(traced().new_page(), token))


def test_an_expired_ticket_is_refused(world, traced):
    expired = ticket(world, world.student.id, TENANT, age=300, course=str(world.course_id))
    _refused(post_ticket(traced().new_page(), expired))


def test_a_ticket_for_another_tenant_is_refused(world, traced):
    """Signed with the key every tenant's Moodle trusts and addressed to this Moodle, for the
    other tenant's person who shares the Student's mailbox: this Moodle serves one tenant."""
    _refused(post_ticket(traced().new_page(), ticket(world, world.twin.id, world.other.id)))
