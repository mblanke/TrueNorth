"""Browser journeys for the rc11 evidence gaps, in Chromium, against an API that enforces auth.

The rc11 evidence record found five areas with no browser journey: wiki and helpdesk,
resuming a paused exercise, a Student's own AAR, self-enrolment, and a Student's detection
submission. Each journey here does the thing in the SPA and then asserts what was
*persisted*: re-read through the API as the person concerned, and/or after a reload. Where
the gap is about access, the same thing is tried as the role that must be refused, in the
browser and directly against the API.

What is real: Chromium; the production SPA build (``npm run build:prod``); the API with real
bearer-token checks (``AUTH_DISABLED=false``, generic OIDC against a key set this test
serves, tests/integration/_journey_idp.py), so each person's role, tenant and permissions
come from their own ``users`` row, exactly as in a deployment; for the detection journey,
a real OpenSearch with bootstrap's ingest-time pipeline (as test_detection_flow.py).
What is not: Keycloak's own pages (the browser's three Keycloak calls are answered by the
test, as the e2e lane and the Moodle journey do); nginx (tests/integration/_journey_server.py
lays SPA and API out on one origin as nginx does); PostgreSQL (the API runs on its own
SQLite file, as the Moodle journey does); Celery (no broker: the API's dispatches fail fast
and are recorded as not queued, so no scenario injects fire).

Configuration:

    JOURNEY_SPA            the SPA build's ``browser`` directory
                           (default control-plane/web/dist/truenorth-range-web/browser)
    JOURNEY_PORT           where TrueNorth listens (default 18096)
    OPENSEARCH_URL         a disposable OpenSearch, security plugin off (default
                           http://127.0.0.1:19200, the itest stack's); only the detection
                           journey needs it
    BROWSER_JOURNEYS_REQUIRED=1   a missing SPA build, Chromium or OpenSearch fails
                                  instead of skipping (the CI ``journeys`` lane sets it)
    BROWSER_JOURNEYS_ARTIFACTS    a directory for the server log and per-test Playwright
                                  traces (CI uploads it)

Locally:

    (cd control-plane/web && npm ci && npm run build:prod)
    .venv/bin/python -m playwright install chromium
    docker run -d --rm --name tn-journeys-os -p 127.0.0.1:19200:9200 \\
      -e discovery.type=single-node -e DISABLE_SECURITY_PLUGIN=true \\
      -e OPENSEARCH_JAVA_OPTS="-Xms512m -Xmx512m" opensearchproject/opensearch:2.13.0
    .venv/bin/python -m pytest tests/integration/test_browser_journeys.py -m integration -rs
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPA = pathlib.Path(os.getenv("JOURNEY_SPA") or ROOT / "control-plane/web/dist/truenorth-range-web/browser")
PORT = int(os.getenv("JOURNEY_PORT", "18096"))
TN = f"http://localhost:{PORT}"
API = f"http://127.0.0.1:{PORT}/api"
OPENSEARCH = os.getenv("OPENSEARCH_URL", "http://127.0.0.1:19200").rstrip("/")
REQUIRED = os.getenv("BROWSER_JOURNEYS_REQUIRED") == "1"
ARTIFACTS = os.getenv("BROWSER_JOURNEYS_ARTIFACTS", "")
TENANT = uuid.UUID("7e57e57e-0000-4000-8000-0000000000b1")
UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

pytestmark = pytest.mark.integration

C2_DOMAIN = "northwind-update.example"
# One detection objective (ADR 0005) and one deliverable, as test_detection_flow.py.
SCENARIO = f"""name: journey-scenario
version: "1"
range_template: journey
variables:
  c2_domain: {C2_DOMAIN}
timeline: [{{t: "0:00"}}]
objectives:
  - id: detect_c2
    type: detection
    validator: opensearch_query
    params: {{query: 'event_type:http AND url.domain:*{{{{ c2_domain }}}}*', min_hits: 2}}
    points: 40
  - id: write_report
    type: deliverable
    validator: deliverable_check
    points: 10
"""


def _missing(reason: str):
    if REQUIRED:
        pytest.fail(f"BROWSER_JOURNEYS_REQUIRED=1 but {reason}")
    pytest.skip(reason)


def _wait_http(url: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if httpx.get(url, timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        assert time.monotonic() < deadline, f"{url} did not answer within {seconds:.0f}s"
        time.sleep(1)


class Api:
    """TrueNorth's API as ``person``: a bearer token from the test's identity provider and
    the CSRF double-submit the SPA does. What a person can do here is what the API lets
    their users row do; nothing is mocked behind it."""

    def __init__(self, world, person):
        token = world.idp.token(person.keycloak_id, email=person.email)
        self.c = httpx.Client(base_url=API, timeout=60, headers={"Authorization": f"Bearer {token}"})
        self.c.get("/health/live")  # sets the CSRF cookie

    def get(self, path: str, **kw) -> httpx.Response:
        return self.c.get(path, **kw)

    def _csrf(self) -> dict:
        return {"X-CSRF-Token": self.c.cookies.get("truenorth_csrf", "")}

    def post(self, path: str, **kw) -> httpx.Response:
        return self.c.post(path, headers=self._csrf(), **kw)

    def patch(self, path: str, **kw) -> httpx.Response:
        return self.c.patch(path, headers=self._csrf(), **kw)


def ok(resp: httpx.Response, *codes: int):
    assert resp.status_code in (codes or (200, 201)), f"{resp.request.method} {resp.request.url} => {resp.status_code} {resp.text}"
    return resp.json()


# -- the world ------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    if not SPA.joinpath("index.html").is_file():
        _missing(f"no SPA build at {SPA} (cd control-plane/web && npm run build:prod)")
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        _missing("Playwright is not installed (requirements-test.txt; python -m playwright install chromium)")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            pytest.fail(f"port {PORT} is taken; set JOURNEY_PORT")

    sys.path.insert(0, str(HERE))
    from _journey_idp import Idp
    from app.db import Base
    from app.models import Course, Tenant, User, UserRole
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    tmp = tmp_path_factory.mktemp("browser-journeys")
    db_file = tmp / "tn.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    db = session()
    db.add(Tenant(id=TENANT, name="Journey unit", slug=f"journey-{uuid.uuid4().hex[:6]}"))
    db.commit()

    def person(role: UserRole, first: str, last: str) -> User:
        uid = uuid.uuid4()
        u = User(id=uid, keycloak_id=f"kc-{uid}", email=f"{first.lower()}-{uid.hex[:6]}@example.test",
                 display_name=f"{first} {last}", first_name=first, last_name=last, role=role, tenant_id=TENANT,
                 onboarding_state="complete", onboarded_at=datetime.now(UTC))
        db.add(u)
        db.flush()
        return u

    people = SimpleNamespace(
        admin=person(UserRole.admin, "Ada", "Admin"),
        instructor=person(UserRole.instructor, "Ines", "Instructor"),
        range_ops=person(UserRole.range_ops, "Rae", "Rangeops"),
        sam=person(UserRole.student, "Sam", "Student"),
        bo=person(UserRole.student, "Bo", "Student"),
    )

    def course(name: str, *, published: bool = True, meta: dict | None = None) -> Course:
        c = Course(id=uuid.uuid4(), name=name, tenant_id=TENANT, is_published=published,
                   course_meta=json.dumps(meta or {}))
        db.add(c)
        db.flush()
        return c

    courses = SimpleNamespace(
        open=course("Journey Open Course"),
        retired=course("Journey Retired Course", meta={"retired": True}),
        draft=course("Journey Draft Course", published=False),
    )
    db.commit()

    idp = Idp(tmp, f"{TN}/auth/realms/truenorth")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT / "control-plane/api"), str(HERE)]),
        "DATABASE_URL": f"sqlite:///{db_file}",
        "DB_AUTO_CREATE": "false",
        "SEED_DEV_DATA": "false",
        "AUTH_DISABLED": "false",
        "AUTH_BACKEND": "generic_oidc",
        "OIDC_JWKS_URL": f"http://127.0.0.1:{PORT}/test-idp/jwks",
        "OIDC_ISSUER": idp.issuer,
        "OIDC_AUDIENCE": "truenorth-web",
        "CORS_ORIGINS": TN,
        "RATE_LIMIT_ENABLED": "false",
        # No broker: port 1 refuses at once, so a dispatch fails fast and is recorded as
        # not queued (tests/conftest.py does the same for the unit suite).
        "REDIS_URL": "redis://127.0.0.1:1/15",
        "PLATFORM_TENANT_ID": str(TENANT),
        "SEARCH_BACKEND": "opensearch",
        "OPENSEARCH_URL": OPENSEARCH,
        "EXERCISE_CLOCK": "false",
        "SCHEDULER_CLOCK_ENABLED": "false",
        "LAB_SESSIONS_SWEEP": "false",
        "WS_HEARTBEAT": "false",
        "COURSE_PUBLISH_RESUME": "false",
        "MOODLE_RESULTS_PULL_SECONDS": "0",
        "RANGE_OP_REDISPATCH_SECONDS": "0",
        "JOURNEY_SPA_DIST": str(SPA),
        "JOURNEY_IDP_JWKS": str(idp.jwks_file),
    }
    log = (tmp / "server.log").open("w")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "_journey_server:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=HERE, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        _wait_http(f"{API}/health/live", 90)
        w = SimpleNamespace(db=db, idp=idp, tenant=TENANT, courses=courses, server_log=tmp / "server.log",
                            **vars(people))
        admin = Api(w, w.admin)
        w.template = ok(admin.post("/templates", json={
            "name": f"journey-{uuid.uuid4().hex[:6]}", "yaml": "name: journey\nenvironment: enterprise\n",
            "is_public": False}), 201)
        w.scenario = ok(admin.post("/scenarios", json={"name": "journey-scenario", "yaml": SCENARIO,
                                                         "is_public": False}), 201)
        yield w
    finally:
        server.terminate()
        try:
            server.wait(20)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        if ARTIFACTS:
            pathlib.Path(ARTIFACTS).mkdir(parents=True, exist_ok=True)
            pathlib.Path(ARTIFACTS, "truenorth-server.log").write_text((tmp / "server.log").read_text())


@pytest.fixture(scope="module")
def browser(world):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 — Playwright's missing-browser error is generic
            _missing(f"Chromium for Playwright is not installed ({exc.__class__.__name__}): "
                     "python -m playwright install chromium")
        yield b
        b.close()


@pytest.fixture
def visit(browser, world, request):
    """``visit(person, path)``: a new browser profile signed in as ``person``, on ``path``.
    With BROWSER_JOURNEYS_ARTIFACTS set, each profile's trace is written there."""
    from _journey_idp import signed_in_context

    made = []

    def make(person, path: str):
        ctx = signed_in_context(browser, world.idp, person, TN)
        ctx.set_default_timeout(30_000)
        if ARTIFACTS:
            ctx.tracing.start(screenshots=True, snapshots=True)
        made.append(ctx)
        page = ctx.new_page()
        page.goto(path)
        return page

    yield make
    for n, ctx in enumerate(made):
        if ARTIFACTS:
            pathlib.Path(ARTIFACTS).mkdir(parents=True, exist_ok=True)
            ctx.tracing.stop(path=str(pathlib.Path(ARTIFACTS) / f"{request.node.name}-{n}.zip"))
        ctx.close()


def title(page, name):
    """The routed page's <h1>, inside the shell's main region."""
    return page.get_by_role("main").get_by_role("heading", level=1, name=name)


# -- setup helpers (each as the role that would do it) -------------------------------------
def make_exercise(world, name: str) -> dict:
    """A range and an exercise on the shared scenario, made by the admin through the API."""
    admin = Api(world, world.admin)
    rng = ok(admin.post("/ranges", json={"name": f"{name}-range", "template_id": world.template["id"]}), 201)
    return ok(admin.post("/exercises", json={"name": name, "range_id": rng["id"],
                                             "scenario_id": world.scenario["id"], "max_score": 50}), 201)


def book(world, exercise_id: str, student) -> None:
    """A course booked over the exercise (a live calendar booking) with ``student`` assigned
    to it by staff: that is what makes a Student a participant (app/exercise_participants.py)."""
    from app.enrollment import ensure_enrollment
    from app.models import Course
    from app.scheduler.models import EventState, ScheduledEvent

    db = world.db
    cls = Course(id=uuid.uuid4(), name=f"Class for {exercise_id[:8]}", tenant_id=world.tenant, is_published=True)
    db.add(cls)
    db.flush()
    now = datetime.now(UTC)
    db.add(ScheduledEvent(name="lesson", tenant_id=world.tenant, course_id=cls.id, state=EventState.active,
                          start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=3),
                          exercise_id=uuid.UUID(exercise_id)))
    ensure_enrollment(db, user_id=student.id, course_id=cls.id, tenant_id=world.tenant, source="assigned")
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════════════════
# (1a) Wiki: create a space and a page, edit it, read its history; a staff-only space is
#      invisible to a Student.
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_wiki_space_page_edit_history_and_staff_space_hidden_from_student(world, visit):
    from playwright.sync_api import expect

    # The admin creates a staff-only space in the browser (WIKI_ADMIN).
    admin = visit(world.admin, "/wiki")
    expect(title(admin, "Wiki")).to_be_visible()
    admin.get_by_role("button", name="New space").click()
    admin.get_by_label("Name", exact=True).fill("Journey Runbooks")
    expect(admin.get_by_label("Address (lowercase letters, numbers and dashes)")).to_have_value("journey-runbooks")
    admin.get_by_label("Who can read it").select_option("staff")
    admin.get_by_role("button", name="Create space").click()
    expect(admin.get_by_role("link", name="Journey Runbooks")).to_be_visible()
    admin.reload()
    expect(admin.get_by_role("link", name="Journey Runbooks")).to_be_visible()
    space = ok(Api(world, world.admin).get("/wiki/spaces/journey-runbooks"))
    assert space["visibility"] == "staff", space

    # And an open space with a published page, which the Student must still see.
    a = Api(world, world.admin)
    ok(a.post("/wiki/spaces", json={"name": "Journey Handbook", "slug": "journey-handbook"}), 201)
    ok(a.post("/wiki/spaces/journey-handbook/pages", json={"title": "Welcome", "body": "Hello."}), 201)

    # An instructor (WIKI_EDIT) writes a page, edits it, and reads its history.
    ins = visit(world.instructor, "/wiki/journey-runbooks/new")
    expect(title(ins, "New page")).to_be_visible()
    ins.get_by_label("Title", exact=True).fill("Resuming a paused exercise")
    ins.get_by_label("Page content").fill("Step one: open the exercise.")
    ins.get_by_role("button", name="Save", exact=True).click()
    expect(ins).to_have_url(re.compile(rf"/wiki/journey-runbooks/{UUID_RE}$"))
    page_id = ins.url.rsplit("/", 1)[1]
    expect(title(ins, "Resuming a paused exercise")).to_be_visible()
    expect(ins.get_by_text("revision 1")).to_be_visible()

    ins.get_by_role("link", name="Edit", exact=True).click()
    expect(title(ins, "Edit page")).to_be_visible()
    body = ins.get_by_label("Page content")
    expect(body).to_have_value("Step one: open the exercise.")
    body.fill("Step one: open the exercise.\n\nStep two: press Resume.")
    ins.get_by_label("What changed? (optional)").fill("added the resume step")
    ins.get_by_role("button", name="Save", exact=True).click()
    expect(ins).to_have_url(re.compile(rf"/wiki/journey-runbooks/{page_id}$"))
    expect(ins.get_by_text("revision 2")).to_be_visible()
    ins.reload()
    expect(ins.get_by_text("Step two: press Resume.")).to_be_visible()

    ins.get_by_role("link", name="History").click()
    expect(title(ins, "History")).to_be_visible()
    rows = ins.locator("table tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("added the resume step")
    expect(rows.first).to_contain_text("Ines Instructor")

    revisions = ok(Api(world, world.instructor).get(f"/wiki/pages/{page_id}/revisions"))
    assert [r["revision_number"] for r in revisions] == [2, 1], revisions
    assert revisions[0]["edit_summary"] == "added the resume step"
    stored = ok(Api(world, world.instructor).get(f"/wiki/pages/{page_id}"))
    assert stored["body"] == "Step one: open the exercise.\n\nStep two: press Resume.", stored

    # A Student sees the open space and nothing of the staff one: not listed, not by link.
    sam = visit(world.sam, "/wiki")
    expect(title(sam, "Wiki")).to_be_visible()
    expect(sam.get_by_role("link", name="Journey Handbook")).to_be_visible()
    expect(sam.get_by_role("link", name="Journey Runbooks")).to_have_count(0)
    expect(sam.get_by_role("button", name="New space")).to_have_count(0)
    sam.goto(f"/wiki/journey-runbooks/{page_id}")
    expect(sam.get_by_text("This space doesn't exist, or you don't have access to it.")).to_be_visible()
    expect(sam.get_by_text("Step two: press Resume.")).to_have_count(0)

    student = Api(world, world.sam)
    assert "journey-runbooks" not in {s["slug"] for s in ok(student.get("/wiki/spaces"))}
    assert student.get("/wiki/spaces/journey-runbooks").status_code == 404
    assert student.get(f"/wiki/pages/{page_id}").status_code == 404
    assert student.get(f"/wiki/pages/{page_id}/revisions").status_code == 403
    assert page_id not in {h["page_id"] for h in ok(student.get("/wiki/search", params={"q": "Resume"}))}
    assert student.post("/wiki/spaces/journey-runbooks/pages", json={"title": "x"}).status_code == 403


# ══════════════════════════════════════════════════════════════════════════════════════════
# (1b) Helpdesk: a Student opens a ticket, staff assign it, the assignee finds it in their
#      notification inbox; another Student cannot see it.
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_helpdesk_ticket_assigned_reaches_the_assignee_inbox_and_stays_private(world, visit):
    from playwright.sync_api import expect

    subject = f"dc01 will not boot past BIOS {uuid.uuid4().hex[:4]}"
    sam = visit(world.sam, "/support/new")
    expect(title(sam, "Report a problem")).to_be_visible()
    sam.get_by_label("What's wrong?").fill(subject)
    sam.get_by_label("Details", exact=True).fill("It stops at the vendor logo.")
    sam.get_by_role("button", name="Submit", exact=True).click()
    expect(sam).to_have_url(re.compile(rf"/support/{UUID_RE}$"))
    ticket_id = sam.url.rsplit("/", 1)[1]
    expect(title(sam, subject)).to_be_visible()
    mine = ok(Api(world, world.sam).get("/tickets"))
    assert [t["id"] for t in mine] == [ticket_id], mine

    # An instructor (TICKET_WORK) assigns it to range ops.
    ins = visit(world.instructor, f"/support/{ticket_id}")
    expect(title(ins, subject)).to_be_visible()
    with ins.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith(f"/tickets/{ticket_id}")) as resp:
        ins.get_by_label("Assignee").select_option(label="Rae Rangeops")
    assert resp.value.status == 200, resp.value.text()
    ins.reload()
    expect(ins.get_by_label("Assignee")).to_have_value(str(world.range_ops.id))
    assert ok(Api(world, world.instructor).get(f"/tickets/{ticket_id}"))["assignee_id"] == str(world.range_ops.id)

    # The assignee finds it in their inbox (the toolbar bell) and opens it from there.
    rae = visit(world.range_ops, "/dashboard")
    bell = rae.get_by_role("button", name=re.compile(r"\d+ unread notifications"))
    expect(bell).to_be_visible()
    bell.click()
    item = rae.get_by_role("dialog", name="Notifications").get_by_role("button", name=re.compile(r"assigned to you"))
    expect(item).to_have_count(1)
    expect(item).to_contain_text(subject)
    item.click()
    expect(rae).to_have_url(re.compile(rf"/support/{ticket_id}$"))
    expect(title(rae, subject)).to_be_visible()
    inbox = ok(Api(world, world.range_ops).get("/notifications"))
    assigned = [n for n in inbox if n["kind"] == "ticket_assigned"]
    assert len(assigned) == 1 and assigned[0]["link"] == f"/support/{ticket_id}", inbox
    assert assigned[0]["read"] is True, "opening it from the bell marks it read, on the server"

    # Another Student: not in their list, not by link, not through the API, not in their inbox.
    bo = visit(world.bo, "/support")
    expect(title(bo, "Support")).to_be_visible()
    expect(bo.get_by_text(subject)).to_have_count(0)
    bo.goto(f"/support/{ticket_id}")
    expect(bo.get_by_text("This ticket doesn't exist, or you don't have access to it.")).to_be_visible()
    expect(bo.get_by_text("It stops at the vendor logo.")).to_have_count(0)
    other = Api(world, world.bo)
    assert other.get(f"/tickets/{ticket_id}").status_code == 404
    assert other.get(f"/tickets/{ticket_id}/comments").status_code == 404
    assert ticket_id not in {t["id"] for t in ok(other.get("/tickets", params={"scope": "all"}))}
    assert not [n for n in ok(other.get("/notifications")) if ticket_id in (n.get("link") or "")]
    assert other.patch(f"/tickets/{ticket_id}", json={"subject": "mine now"}).status_code == 404


# ══════════════════════════════════════════════════════════════════════════════════════════
# (2) Exercises: Resume on a paused exercise moves it to running, once, however often it is
#     clicked; a Student has no Resume and the API refuses theirs.
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_resume_a_paused_exercise_once_and_only_as_staff(world, visit):
    from playwright.sync_api import expect

    name = f"journey-resume-{uuid.uuid4().hex[:4]}"
    ex = make_exercise(world, name)
    staff = Api(world, world.instructor)
    ok(staff.post(f"/exercises/{ex['id']}/start"))
    ok(staff.post(f"/exercises/{ex['id']}/pause"))

    # The Student sees the paused exercise and no Resume; the API refuses a direct one.
    sam = visit(world.sam, "/exercises")
    sam_row = sam.get_by_role("row").filter(has_text=name)
    expect(sam_row.locator(".status-chip")).to_have_text("paused")
    expect(sam_row.get_by_role("button", name="Resume exercise")).to_have_count(0)
    assert Api(world, world.sam).post(f"/exercises/{ex['id']}/resume").status_code == 403
    assert ok(staff.get(f"/exercises/{ex['id']}"))["state"] == "paused"

    # The instructor double-clicks Resume: one request, state running, no error.
    ins = visit(world.instructor, "/exercises")
    row = ins.get_by_role("row").filter(has_text=name)
    expect(row.locator(".status-chip")).to_have_text("paused")
    sent: list[int] = []
    ins.on("response", lambda r: sent.append(r.status)
           if r.request.method == "POST" and r.url.endswith(f"/exercises/{ex['id']}/resume") else None)
    row.get_by_role("button", name="Resume exercise").dblclick()
    expect(row.locator(".status-chip")).to_have_text("running")
    expect(row.get_by_role("button", name="Pause exercise")).to_be_visible()
    expect(row.get_by_role("button", name="Resume exercise")).to_have_count(0)
    ins.wait_for_load_state("networkidle")
    assert sent == [200], f"Resume sent {len(sent)} request(s): {sent}"
    expect(ins.get_by_text("Resume failed")).to_have_count(0)

    ins.reload()
    expect(ins.get_by_role("row").filter(has_text=name).locator(".status-chip")).to_have_text("running")
    assert ok(staff.get(f"/exercises/{ex['id']}"))["state"] == "running"
    # A late second resume (another tab) is refused by the server, not applied twice.
    assert staff.post(f"/exercises/{ex['id']}/resume").status_code == 409


# ══════════════════════════════════════════════════════════════════════════════════════════
# (3) AAR: a Student opens the report of an exercise they took part in, and not another
#     Student's.
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_student_opens_own_aar_and_not_another_students(world, visit):
    from playwright.sync_api import expect

    staff = Api(world, world.instructor)
    runs = {}
    for who in ("sam", "bo"):
        name = f"journey-aar-{who}-{uuid.uuid4().hex[:4]}"
        ex = make_exercise(world, name)
        book(world, ex["id"], getattr(world, who))
        ok(staff.post(f"/exercises/{ex['id']}/start"))
        ok(staff.post(f"/exercises/{ex['id']}/complete"))
        ok(staff.post(f"/exercises/{ex['id']}/aar/generate"), 201)
        runs[who] = (ex["id"], name)
    own, own_name = runs["sam"]
    theirs, their_name = runs["bo"]

    sam = visit(world.sam, f"/scoring?exercise={own}")
    expect(title(sam, "Scoring & After Action Review")).to_be_visible()
    expect(sam.get_by_test_id("aar-view")).to_be_visible()
    report = sam.frame_locator("iframe[title='After-action report']")
    expect(report.get_by_role("heading", level=1)).to_have_text(f"After-Action Report: {own_name}")

    sam.goto(f"/scoring?exercise={theirs}")
    expect(sam.get_by_test_id("aar-none")).to_be_visible()
    expect(sam.get_by_test_id("aar-view")).to_have_count(0)
    expect(sam.locator("iframe[title='After-action report']")).to_have_count(0)

    student = Api(world, world.sam)
    assert ok(student.get(f"/exercises/{own}/aar"))["exercise_id"] == own
    for path in ("aar", "aar/html", "aar/pdf"):
        assert student.get(f"/exercises/{theirs}/{path}").status_code == 404, path
    assert student.post(f"/exercises/{own}/aar/generate").status_code == 403
    # Staff review every run.
    assert staff.get(f"/exercises/{theirs}/aar").status_code == 200


# ══════════════════════════════════════════════════════════════════════════════════════════
# (4) Self-enrolment: a Student enrols on an open course (double click safe, survives a
#     reload); a retired course and a draft are refused; nobody enrols someone else.
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_self_enrolment_once_and_refusals(world, visit):
    from playwright.sync_api import expect

    course = str(world.courses.open.id)
    sam = visit(world.sam, f"/learning/courses/{course}")
    enrol = sam.get_by_test_id("enrol")
    expect(enrol).to_be_visible()
    sent: list[int] = []
    sam.on("response", lambda r: sent.append(r.status)
           if r.request.method == "POST" and r.url.endswith(f"/courses/{course}/enroll") else None)
    enrol.dblclick()
    expect(sam.get_by_test_id("enrolled")).to_be_visible()
    sam.wait_for_load_state("networkidle")
    assert sent in ([201], [201, 200]), f"Enrol answered {sent}"
    sam.reload()
    expect(sam.get_by_test_id("enrolled")).to_be_visible()
    expect(sam.get_by_test_id("enrol")).to_have_count(0)

    student = Api(world, world.sam)
    mine = [e for e in ok(student.get("/courses/enrollments/mine")) if e["course_id"] == course]
    assert len(mine) == 1, mine
    def enrol_api(api, course_id: str, user) -> httpx.Response:
        return api.post(f"/courses/{course_id}/enroll", json={"user_id": str(user.id), "course_id": course_id})

    again = enrol_api(student, course, world.sam)
    assert again.status_code == 200 and again.json()["id"] == mine[0]["id"], again.text

    # A retired course shows the server's refusal and records nothing.
    retired = str(world.courses.retired.id)
    sam.goto(f"/learning/courses/{retired}")
    sam.get_by_test_id("enrol").click()
    expect(sam.get_by_role("alert").filter(has_text="retired")).to_be_visible()
    expect(sam.get_by_test_id("enrolled")).to_have_count(0)
    # A draft is not there to enrol on; and a Student cannot enrol someone else.
    draft = str(world.courses.draft.id)
    assert enrol_api(student, draft, world.sam).status_code == 404
    assert enrol_api(student, course, world.bo).status_code in (403, 404)
    ids = {e["course_id"] for e in ok(student.get("/courses/enrollments/mine"))}
    assert retired not in ids and draft not in ids
    assert course not in {e["course_id"] for e in ok(Api(world, world.bo).get("/courses/enrollments/mine"))}


# ══════════════════════════════════════════════════════════════════════════════════════════
# (5) Detection (ADR 0005): a Student who takes part submits a detection query in the
#     exercise and it is recorded and credited; a Student who does not take part is refused.
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def event_store():
    try:
        httpx.get(OPENSEARCH, timeout=3.0)
    except httpx.HTTPError:
        _missing(f"no OpenSearch at {OPENSEARCH} (set OPENSEARCH_URL)")
    with httpx.Client(base_url=OPENSEARCH, timeout=30) as c:
        yield c


@pytest.fixture
def range_index(event_store):
    """Bootstrap's ingest-time pipeline and range template for one range id, as
    test_detection_flow.py; removed after."""
    sys.path.insert(0, str(ROOT))
    bootstrap = importlib.import_module("telemetry.pipelines.bootstrap")
    tag = uuid.uuid4().hex[:10]
    pipeline, template = f"tn-journey-ingested-at-{tag}", f"tn-journey-range-{tag}"
    made: list[str] = []

    def _for(range_id: str) -> str:
        body = copy.deepcopy(bootstrap.RANGE_TEMPLATE)
        body["index_patterns"] = [f"range-{range_id}*"]
        body["priority"] = 500
        body["template"]["settings"]["index.final_pipeline"] = pipeline
        event_store.put(
            f"/_ingest/pipeline/{pipeline}", json=bootstrap.INGEST_PIPELINES[bootstrap.INGESTED_AT_PIPELINE]
        ).raise_for_status()
        event_store.put(f"/_index_template/{template}", json=body).raise_for_status()
        made.append(range_id)
        return f"range-{range_id}"

    yield _for
    for range_id in made:
        event_store.delete(f"/range-{range_id}")
    event_store.delete(f"/_index_template/{template}")
    event_store.delete(f"/_ingest/pipeline/{pipeline}")


def test_student_detection_is_recorded_and_credited(world, visit, event_store, range_index):
    from playwright.sync_api import expect

    name = f"journey-detect-{uuid.uuid4().hex[:4]}"
    ex = make_exercise(world, name)
    book(world, ex["id"], world.sam)
    index = range_index(ex["range_id"])
    staff = Api(world, world.instructor)
    ok(staff.post(f"/exercises/{ex['id']}/start"))
    # The range's telemetry after the start: the attack's beacons among benign traffic.
    events = [{"event_type": "http", "url": {"domain": f"cdn.{C2_DOMAIN}"}} for _ in range(3)]
    events += [{"event_type": "http", "url": {"domain": f"site{i}.example"}} for i in range(4)]
    ok(Api(world, world.admin).post(f"/telemetry/{ex['range_id']}/events", json=events), 202)
    event_store.post(f"/{index}/_refresh").raise_for_status()

    sam = visit(world.sam, f"/exercises/{ex['id']}")
    expect(sam.get_by_role("heading", name=name)).to_be_visible()
    sam.get_by_role("button", name="Submit detection").click()
    sam.get_by_label("Detection query").fill(f"url.domain:*{C2_DOMAIN}")
    sam.get_by_role("button", name="Submit", exact=True).click()
    expect(sam.get_by_role("status").filter(has_text="Achieved: your query found the attack.")).to_be_visible()

    sam.reload()
    expect(sam.get_by_role("heading", name=name)).to_be_visible()
    expect(sam.get_by_text("40 / 50")).to_be_visible()
    sam.get_by_role("button", name="Attempts (1)").click()
    expect(sam.locator("table td.v")).to_have_text(["achieved"])
    expect(sam.get_by_role("button", name="Submit detection")).to_have_count(0)  # achieved: closed

    attempts = ok(Api(world, world.sam).get(f"/exercises/{ex['id']}/detections"))
    assert [(a["verdict"], a["user_id"], a["events_matched"]) for a in attempts] == [
        ("achieved", str(world.sam.id), 3)], attempts
    objectives = {o["ref_id"]: o for o in ok(staff.get(f"/exercises/{ex['id']}/objectives"))}
    assert objectives["detect_c2"]["achieved"] is True
    assert '"source": "student_detection"' in (objectives["detect_c2"]["evidence"] or "")
    assert ok(staff.get(f"/exercises/{ex['id']}"))["total_score"] == 40

    # A Student of the tenant who does not take part in this run: refused, nothing recorded,
    # and they do not see Sam's attempt.
    other = Api(world, world.bo)
    refused = other.post(f"/exercises/{ex['id']}/objectives/write_report/detections",
                         json={"query": f"url.domain:*{C2_DOMAIN}"})
    assert refused.status_code == 403, refused.text
    assert ok(other.get(f"/exercises/{ex['id']}/detections")) == []
    assert len(ok(staff.get(f"/exercises/{ex['id']}/detections"))) == 1
