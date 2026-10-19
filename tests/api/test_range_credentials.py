"""Range credentials (ADR 0012 §12-14): declared, sealed, role-filtered, audited, documented.

``GET /ranges/{id}/credentials``:

* admin, instructor, range_ops: every account of a range in their tenant;
* a Student: only ``student_visible`` accounts a build set (``applied``), and only on a
  range they take part in (a running exercise booked to a course they are rostered on).
  Any other range is 404, as is a range with an exercise nobody booked. Lab sessions are
  not covered (their ranges hold no credentials; the worker gives their VMs random
  passwords: tests/worker/test_range_accounts_deploy.py);
* an observer: 403;
* another tenant's range: 404 for everyone.

Rows are written when the range is created and when a build is accepted; generated
passwords are made once and kept; rows are sealed at rest, and so are typed-in passwords
in stored templates; every read is audited without the password. The range's wiki page is
in a system-owned, always staff-only space, holds tokens rather than passwords, and keeps
its notes.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import pytest
from _shared import act_as, real_tenant, real_user
from app import secretbox
from app.models import (
    AuditLog,
    Course,
    Enrollment,
    EnrollmentStatus,
    Exercise,
    ExerciseState,
    Range,
    RangeState,
    Scenario,
    Template,
    UserRole,
)
from app.models_wiki import WikiPage, WikiRevision, WikiSpace
from app.range_credentials import RangeCredential, wiki_doc
from app.range_credentials import accounts as account_rules
from app.range_credentials import service as cred_service
from app.range_ops import RangeOperation
from app.range_ops import service as ops
from app.scheduler.models import EventState, ScheduledEvent
from sqlalchemy import text

DC_PW = "Dc-Literal-1!"
TEMPLATE_YAML = f"""
name: creds-lab
network:
  vlans:
    - {{id: 100, name: lan, cidr: 10.70.0.0/24, description: Office}}
nodes:
  - {{id: dc01, name: Domain Controller, os: windows-server-2022, vlan: lan, ip: 10.70.0.10}}
  - {{id: web01, name: Web, os: ubuntu-2404, vlan: lan, ip: 10.70.0.20}}
accounts:
  - {{machine: dc01, username: Administrator, password: "{DC_PW}", role: admin, student_visible: false}}
  - {{machine: web01, username: ubuntu, generate: true, role: admin, student_visible: false}}
  - {{machine: Web, username: analyst, generate: true, student_visible: true, notes: "Student login"}}
"""


@pytest.fixture(autouse=True)
def background_session(db_session, monkeypatch):
    """Template-update regeneration runs after the response, in a session of its own; here
    that is the test's (TestClient runs background tasks before it returns)."""
    monkeypatch.setattr(cred_service, "session_factory", lambda: nullcontext(db_session))


@pytest.fixture
def world(db_session):
    a, b = real_tenant(db_session, "cred-a"), real_tenant(db_session, "cred-b")
    w = {"a": a, "b": b}
    for name, role, tenant in (("admin", UserRole.admin, a), ("instructor", UserRole.instructor, a),
                               ("range_ops", UserRole.range_ops, a), ("observer", UserRole.observer, a),
                               ("student", UserRole.student, a), ("classmate", UserRole.student, a),
                               ("foreign_instructor", UserRole.instructor, b),
                               ("foreign_student", UserRole.student, b)):
        w[name] = real_user(db_session, role, tenant.id)
    return w


def _template(db, tenant_id, yaml_text: str = TEMPLATE_YAML, public: bool = False) -> Template:
    t = Template(id=uuid.uuid4(), name="creds-lab", version="2.1", yaml=yaml_text, tenant_id=tenant_id,
                 is_public=public)
    db.add(t)
    db.flush()
    return t


def _create_range(client, db, world, name: str = "Creds Range") -> Range:
    tmpl = _template(db, world["a"].id)
    act_as(world["instructor"])
    resp = client.post("/ranges", json={"name": name, "template_id": str(tmpl.id)})
    assert resp.status_code == 201, resp.text
    return db.get(Range, uuid.UUID(resp.json()["id"]))


def _creds(client, rng):
    return client.get(f"/ranges/{rng.id}/credentials")


def _accounts(resp) -> dict[tuple[str, str], dict]:
    return {(a["machine"], a["username"]): a for a in resp.json()["accounts"]}


def _apply(db, rng, *usernames) -> None:
    """What the worker records after a vSphere build set the accounts (all, or these)."""
    for row in db.query(RangeCredential).filter(RangeCredential.range_id == rng.id):
        if not usernames or row.username in usernames:
            row.applied = True
    db.commit()


def _on_the_course(db, rng, *students) -> Exercise:
    ex = _exercise(db, rng)
    _book(db, ex, list(students))
    return ex


def _exercise(db, rng, state=ExerciseState.running) -> Exercise:
    s = Scenario(id=uuid.uuid4(), name="s", yaml="id: s\n", tenant_id=rng.tenant_id)
    db.add(s)
    db.flush()
    ex = Exercise(id=uuid.uuid4(), name="e", range_id=rng.id, scenario_id=s.id, tenant_id=rng.tenant_id, state=state,
                  started_at=datetime.now(UTC) - timedelta(minutes=5))
    db.add(ex)
    db.flush()
    return ex


def _book(db, ex, students) -> None:
    course = Course(id=uuid.uuid4(), name="Class", tenant_id=ex.tenant_id)
    db.add(course)
    db.flush()
    now = datetime.now(UTC)
    db.add(ScheduledEvent(name="lesson", tenant_id=ex.tenant_id, range_id=ex.range_id, exercise_id=ex.id,
                          course_id=course.id, state=EventState.active, start_time=now - timedelta(hours=1),
                          end_time=now + timedelta(hours=2)))
    for s in students:
        db.add(Enrollment(user_id=uuid.UUID(s.id), course_id=course.id, tenant_id=ex.tenant_id,
                          status=EnrollmentStatus.enrolled))
    db.flush()


# ── Roles ───────────────────────────────────────────────────────────────
class TestWhoSeesWhat:
    @pytest.mark.parametrize("role", ["admin", "instructor", "range_ops"])
    def test_staff_see_every_account_with_its_password(self, client, db_session, world, role):
        rng = _create_range(client, db_session, world)
        act_as(world[role])
        resp = _creds(client, rng)
        assert resp.status_code == 200, resp.text
        assert resp.headers["cache-control"] == "no-store"
        body = resp.json()
        assert body["scope"] == "all" and body["range_id"] == str(rng.id)
        accts = _accounts(resp)
        assert set(accts) == {("dc01", "Administrator"), ("web01", "ubuntu"), ("web01", "analyst")}
        assert accts[("dc01", "Administrator")]["password"] == DC_PW
        assert accts[("dc01", "Administrator")]["generated"] is False
        gen = accts[("web01", "analyst")]
        assert gen["generated"] is True and gen["student_visible"] is True and gen["notes"] == "Student login"
        assert len(gen["password"]) == account_rules.GENERATED_LENGTH
        assert {a["applied"] for a in accts.values()} == {False}  # nothing built yet: staff see them flagged

    def test_an_observer_is_refused(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        act_as(world["observer"])
        assert _creds(client, rng).status_code == 403

    def test_a_student_on_no_part_of_the_range_gets_404(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        act_as(world["student"])
        assert _creds(client, rng).status_code == 404

    def test_a_student_sees_only_handed_over_accounts_the_build_set(self, client, db_session, world):
        """M2: an account TrueNorth did not set (not built yet, a Windows account other than
        Administrator, an appliance, a non-vSphere backend) is never shown to a Student."""
        rng = _create_range(client, db_session, world)
        _on_the_course(db_session, rng, world["student"])
        act_as(world["student"])
        resp = _creds(client, rng)
        assert resp.status_code == 200 and resp.json()["accounts"] == []  # handed over, but not set
        _apply(db_session, rng)
        resp = _creds(client, rng)
        assert resp.json()["scope"] == "student"
        assert list(_accounts(resp)) == [("web01", "analyst")]
        assert DC_PW not in resp.text

    def test_staff_see_unapplied_accounts_flagged(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        _apply(db_session, rng, "Administrator")
        act_as(world["range_ops"])
        accts = _accounts(_creds(client, rng))
        assert accts[("dc01", "Administrator")]["applied"] is True
        assert accts[("web01", "analyst")]["applied"] is False

    def test_a_build_acceptance_resets_applied(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        _apply(db_session, rng)
        act_as(world["instructor"])
        assert client.post(f"/ranges/{rng.id}/provision").status_code == 202
        assert {a["applied"] for a in _creds(client, rng).json()["accounts"]} == {False}

    def test_a_rostered_student_of_a_running_booked_exercise(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        _book(db_session, _exercise(db_session, rng), [world["student"]])
        _apply(db_session, rng)
        act_as(world["student"])
        resp = _creds(client, rng)
        assert resp.status_code == 200
        assert [a["username"] for a in resp.json()["accounts"]] == ["analyst"]
        act_as(world["classmate"])  # same tenant, not on the course
        assert _creds(client, rng).status_code == 404

    def test_an_exercise_nobody_booked_seats_no_student(self, client, db_session, world):
        """Stricter than telemetry reads: no roster, no Student credentials (as ADR 0005)."""
        rng = _create_range(client, db_session, world)
        _exercise(db_session, rng)
        act_as(world["student"])
        assert _creds(client, rng).status_code == 404

    def test_a_finished_exercise_no_longer_counts(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        _book(db_session, _exercise(db_session, rng, ExerciseState.completed), [world["student"]])
        act_as(world["student"])
        assert _creds(client, rng).status_code == 404

    @pytest.mark.parametrize("who", ["foreign_instructor", "foreign_student"])
    def test_another_tenant_gets_404(self, client, db_session, world, who):
        rng = _create_range(client, db_session, world)
        act_as(world[who])
        assert _creds(client, rng).status_code == 404

    def test_an_unknown_range_is_404(self, client, world):
        act_as(world["instructor"])
        assert client.get(f"/ranges/{uuid.uuid4()}/credentials").status_code == 404


# ── Storage, audit, lifecycle ───────────────────────────────────────────
class TestStorageAndAudit:
    def test_passwords_are_sealed_at_rest(self, client, db_session, world):
        _create_range(client, db_session, world)
        stored = db_session.execute(text("SELECT password FROM range_credentials")).scalars().all()
        assert len(stored) == 3
        assert all(v.startswith("tnsec:v1:") for v in stored)
        assert DC_PW not in " ".join(stored)

    def test_every_read_is_audited_without_the_password(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        _on_the_course(db_session, rng, world["student"])
        _apply(db_session, rng)
        for who in ("range_ops", "student"):
            act_as(world[who])
            assert _creds(client, rng).status_code == 200
        rows = db_session.query(AuditLog).filter(AuditLog.action == "range_credentials_read").all()
        assert {(str(r.user_id), r.resource_id) for r in rows} == {
            (world["range_ops"].id, str(rng.id)), (world["student"].id, str(rng.id))}
        assert {r.tenant_id for r in rows} == {world["a"].id}
        passwords = [a.password for a in db_session.query(RangeCredential).all()]
        for r in rows:
            assert all(p not in (r.detail or "") for p in passwords)
        assert any("scope=student; accounts=1" in r.detail for r in rows)

    def test_generated_passwords_are_made_once_and_survive_a_rebuild(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        act_as(world["instructor"])
        before = _accounts(_creds(client, rng))
        assert client.post(f"/ranges/{rng.id}/provision").status_code == 202  # allocation: sync again
        after = _accounts(_creds(client, rng))
        assert before == after

    def test_an_unbuilt_ranges_rows_follow_a_template_edit(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        act_as(world["instructor"])
        generated = _accounts(_creds(client, rng))[("web01", "analyst")]["password"]
        tmpl = db_session.get(Template, rng.template_id)
        new_yaml = TEMPLATE_YAML.replace(DC_PW, "Dc-Changed-2!").replace(
            '  - {machine: web01, username: ubuntu, generate: true, role: admin, student_visible: false}\n', "")
        act_as(world["range_ops"])  # template:update
        assert client.put(f"/templates/{tmpl.id}", json={"yaml": new_yaml}).status_code == 200
        accts = _accounts(_creds(client, rng))
        assert set(accts) == {("dc01", "Administrator"), ("web01", "analyst")}
        assert accts[("dc01", "Administrator")]["password"] == "Dc-Changed-2!"
        assert accts[("web01", "analyst")]["password"] == generated

    def test_a_template_edit_reaches_every_tenants_ranges_of_it(self, client, db_session, world):
        """Low (b): after the response, across tenants: another tenant's range built from a
        public template is re-synced and re-documented in its own tenant's space."""
        tmpl = _template(db_session, world["a"].id, public=True)
        theirs = Range(name="Their range", template_id=tmpl.id, tenant_id=world["b"].id, state=RangeState.created)
        db_session.add(theirs)
        db_session.commit()
        act_as(world["range_ops"])
        assert client.put(f"/templates/{tmpl.id}",
                          json={"yaml": TEMPLATE_YAML.replace(DC_PW, "Dc-Changed-2!")}).status_code == 200
        rows = db_session.query(RangeCredential).filter(RangeCredential.range_id == theirs.id).all()
        assert {r.tenant_id for r in rows} == {world["b"].id} and len(rows) == 3
        assert next(r.password for r in rows if r.username == "Administrator") == "Dc-Changed-2!"
        page = db_session.query(WikiPage).filter(WikiPage.tags.contains(f"range:{theirs.id}")).one()
        assert db_session.get(WikiSpace, page.space_id).tenant_id == world["b"].id

    def test_a_failing_background_refresh_never_fails_the_template_edit(self, client, db_session, world,
                                                                       monkeypatch, caplog):
        rng = _create_range(client, db_session, world)

        def no_database():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(cred_service, "session_factory", no_database)
        caplog.set_level(logging.WARNING)
        act_as(world["range_ops"])
        tmpl = db_session.get(Template, rng.template_id)
        assert client.put(f"/templates/{tmpl.id}", json={"name": "renamed"}).status_code == 200
        assert "were not refreshed (RuntimeError)" in caplog.text

    def test_a_built_ranges_rows_do_not_change_under_its_vms(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        rng.state = RangeState.ready
        db_session.commit()
        tmpl = db_session.get(Template, rng.template_id)
        act_as(world["range_ops"])
        assert client.put(f"/templates/{tmpl.id}",
                          json={"yaml": TEMPLATE_YAML.replace(DC_PW, "Dc-Changed-2!")}).status_code == 200
        assert _accounts(_creds(client, rng))[("dc01", "Administrator")]["password"] == DC_PW

    def test_a_range_from_a_template_with_invalid_accounts_is_refused(self, client, db_session, world):
        bad = _template(db_session, world["a"].id,
                        "nodes: [{id: dc01}]\naccounts:\n  - {machine: nope, username: x, generate: true, "
                        "student_visible: false}\n")
        act_as(world["instructor"])
        resp = client.post("/ranges", json={"name": "bad", "template_id": str(bad.id)})
        assert resp.status_code == 422
        assert "not a node or asset" in resp.text

    def test_deleting_the_range_drops_its_rows_and_retires_its_page(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        act_as(world["admin"])  # range:delete
        assert client.delete(f"/ranges/{rng.id}").status_code == 204
        assert db_session.query(RangeCredential).count() == 0
        page = db_session.query(WikiPage).filter(WikiPage.tags.contains(f"range:{rng.id}")).one()
        assert page.deleted_at is not None


# ── Templates ───────────────────────────────────────────────────────────
class TestTemplates:
    def test_invalid_accounts_are_refused_and_reported(self, client, world):
        act_as(world["range_ops"])
        bad = ("nodes: [{id: dc01}]\naccounts:\n"
               "  - {machine: dc01, username: Admin, password: x, generate: true, student_visible: false}\n"
               "  - {machine: dc01, username: admin, generate: true, student_visible: false}\n"
               "  - {machine: dc01, username: Admin, generate: true, student_visible: false}\n"
               "  - {machine: ghost, username: y, generate: true}\n")
        resp = client.post("/templates", json={"name": "bad", "version": "1", "yaml": bad})
        assert resp.status_code == 422
        result = client.post("/templates/validate", json={"yaml": bad}).json()
        messages = " | ".join(f"{e['path']}: {e['message']}" for e in result["errors"])
        assert result["valid"] is False
        assert "exactly one of password and generate" in messages
        assert "declared twice for machine 'dc01'" in messages
        assert "machine 'ghost' is not a node or asset" in messages
        assert "student_visible" in messages

    def test_typed_in_passwords_are_stored_sealed_and_read_back_sealed(self, client, db_session, world):
        """M4/M5: the stored template holds a tnsec: token, never the password; an edit that
        sends the token back keeps it; the credentials sync unseals it."""
        act_as(world["range_ops"])
        resp = client.post("/templates", json={"name": "t", "version": "1", "yaml": TEMPLATE_YAML})
        assert resp.status_code == 201, resp.text
        stored = db_session.get(Template, uuid.UUID(resp.json()["id"])).yaml
        assert DC_PW not in stored and secretbox.PREFIX in stored
        assert DC_PW not in resp.text
        shown = client.get(f"/templates/{resp.json()['id']}").json()["yaml"]
        assert DC_PW not in shown and secretbox.PREFIX in shown  # staff: the sealed token, editable
        assert client.put(f"/templates/{resp.json()['id']}", json={"yaml": shown}).status_code == 200
        rng = client.post("/ranges", json={"name": "r", "template_id": resp.json()["id"]}).json()
        accts = {a["username"]: a for a in client.get(f"/ranges/{rng['id']}/credentials").json()["accounts"]}
        assert accts["Administrator"]["password"] == DC_PW

    def test_a_sealed_value_that_does_not_unseal_is_refused(self, client, world):
        act_as(world["range_ops"])
        forged = TEMPLATE_YAML.replace(DC_PW, "tnsec:v1:not-a-real-token")
        resp = client.post("/templates", json={"name": "t", "version": "1", "yaml": forged})
        assert resp.status_code == 422 and "cannot unseal" in resp.text

    @pytest.mark.parametrize(("yaml_text", "why"), [
        ("nodes: [{id: dc01, os: windows-server-2022}]\naccounts:\n"
         "  - {machine: dc01, username: Administrator, password: short1!, student_visible: false}\n", "8 characters"),
        ("nodes: [{id: dc01, os: windows-11}]\naccounts:\n"
         "  - {machine: dc01, username: Administrator, password: alllowercase1, student_visible: false}\n",
         "three of"),
        ("nodes: [{id: dc01, os: windows-server-2022}]\naccounts:\n"
         "  - {machine: dc01, username: jsmith, password: Xx-JSmith-2026, student_visible: false}\n",
         "contains the account name"),
    ])
    def test_windows_passwords_must_meet_complexity(self, client, world, yaml_text, why):
        """M3: a typed-in password on a Windows machine that Sysprep would reject is a 422."""
        act_as(world["range_ops"])
        resp = client.post("/templates", json={"name": "t", "version": "1", "yaml": yaml_text})
        assert resp.status_code == 422 and why in resp.text
        linux = yaml_text.replace("windows-server-2022", "ubuntu-2404").replace("windows-11", "ubuntu-2404")
        assert client.post("/templates", json={"name": "t", "version": "1", "yaml": linux}).status_code == 201

    @pytest.mark.parametrize("probe", ["escaped_key", "notes_leak"])
    def test_masking_is_decided_on_the_parsed_document(self, probe):
        """The reviewer's probes: an escaped key is still ``password``; a password repeated in
        notes is masked too; YAML the API refuses (anchors) is withheld entirely."""
        doc = {
            "escaped_key": 'name: t\nnodes: [{id: dc01, os: linux}]\naccounts:\n'
                           '  - {machine: dc01, username: root, "passwor\\x64": "Hunter2Hunter2!", '
                           'student_visible: false}\n',
            "notes_leak": 'name: t\nnodes: [{id: dc01, os: linux}]\naccounts:\n'
                          '  - {machine: dc01, username: root, password: "Hunter2Hunter2!", student_visible: false, '
                          'notes: "same as Hunter2Hunter2! on prod"}\n',
        }[probe]
        assert "Hunter2Hunter2!" not in account_rules.redact_yaml(doc)
        anchors = ('name: t\nx: &a {username: Administrator, password: "Hunter2Hunter2!"}\n'
                   'accounts:\n  - {<<: *a, machine: dc01}\n')
        assert account_rules.redact_yaml(anchors) == ""

    def test_passwords_in_a_template_are_hidden_from_who_may_not_read_them(self, client, db_session, world):
        own = _template(db_session, world["a"].id)
        public = _template(db_session, world["a"].id, public=True)
        for who in ("student", "observer"):
            act_as(world[who])
            resp = client.get(f"/templates/{own.id}")
            assert resp.status_code == 200, (who, resp.text)
            assert DC_PW not in resp.json()["yaml"], who
            assert account_rules.REDACTED in resp.json()["yaml"], who
        act_as(world["foreign_instructor"])  # by id, even a public template is its own tenant's only
        assert client.get(f"/templates/{public.id}").status_code == 404

    def test_a_redacted_copy_cannot_be_saved_back(self, client, world):
        act_as(world["range_ops"])
        doc = TEMPLATE_YAML.replace(DC_PW, account_rules.REDACTED)
        assert client.post("/templates", json={"name": "copy", "version": "1", "yaml": doc}).status_code == 422


# ── The generated wiki page (decision 14) ─────────────────────────────────
class TestWikiPage:
    def _page(self, db, rng) -> WikiPage:
        db.expire_all()
        return db.query(WikiPage).filter(WikiPage.tags.contains(f"range:{rng.id}"),
                                         WikiPage.deleted_at.is_(None)).one()

    def _passwords(self, db) -> list[str]:
        return [r.password for r in db.query(RangeCredential).all()]

    def test_it_is_in_a_system_owned_staff_space_and_stores_no_password(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        page = self._page(db_session, rng)
        space = db_session.get(WikiSpace, page.space_id)
        assert (space.slug, space.visibility, space.system_owned, space.tenant_id) == (
            "ranges", "staff", True, world["a"].id)
        assert page.title == "Creds Range"
        body = page.body
        assert body.startswith(wiki_doc.BEGIN) and wiki_doc.END in body and "## Notes" in body
        for expected in ("Domain Controller", "windows-server-2022", "10.70.0.10 (planned)", "| lan | 100 |",
                         "creds-lab 2.1", "analyst", "not set by TrueNorth"):
            assert expected in body, expected
        stored = [body] + [r.body for r in db_session.query(WikiRevision).filter(WikiRevision.page_id == page.id)]
        for pw in self._passwords(db_session):  # Low (c): no wiki row or revision holds one
            assert all(pw not in b for b in stored)
        assert len(wiki_doc.TOKEN_RE.findall(body)) == 3

    def test_staff_read_the_page_with_the_passwords_filled_in(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        page = self._page(db_session, rng)
        act_as(world["instructor"])
        shown = client.get(f"/wiki/pages/{page.id}").json()["body"]
        assert all(pw in shown for pw in self._passwords(db_session)) and DC_PW in shown
        assert not wiki_doc.TOKEN_RE.search(shown)
        assert client.get("/wiki/search", params={"q": DC_PW}).json() == []  # not even for staff
        audit = db_session.query(AuditLog).filter(AuditLog.action == "range_credentials_read").one()
        assert (audit.resource_id, str(audit.user_id)) == (str(rng.id), world["instructor"].id)
        assert "via=wiki" in audit.detail and DC_PW not in audit.detail

    def test_an_editor_cannot_store_passwords_through_the_generated_block(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        page = self._page(db_session, rng)
        act_as(world["instructor"])
        shown = client.get(f"/wiki/pages/{page.id}").json()  # expanded: holds the passwords
        notes = "## Notes\n\nEditor's note.\n"
        tampered = shown["body"].split(wiki_doc.END)[0] + "extra " + wiki_doc.END + "\n\n" + notes
        resp = client.put(f"/wiki/pages/{page.id}", json={"base_revision": shown["revision_number"],
                                                          "body": tampered})
        assert resp.status_code == 200, resp.text
        stored = self._page(db_session, rng).body
        assert stored.split(wiki_doc.END)[0] == page.body.split(wiki_doc.END)[0]  # the block as generated
        assert stored.rstrip().endswith("Editor's note.")
        revs = db_session.query(WikiRevision).filter(WikiRevision.page_id == page.id).all()
        for pw in self._passwords(db_session):
            assert all(pw not in r.body for r in revs)

    @pytest.mark.parametrize("who", ["student", "observer"])
    def test_students_and_observers_cannot_read_it_or_find_it(self, client, db_session, world, who):
        rng = _create_range(client, db_session, world)
        _on_the_course(db_session, rng, world["student"])  # even a Student on the range
        page = self._page(db_session, rng)
        act_as(world[who])
        assert client.get(f"/wiki/pages/{page.id}").status_code == 404
        assert client.get("/wiki/spaces/ranges").status_code == 404
        assert "ranges" not in [s["slug"] for s in client.get("/wiki/spaces").json()]
        for q in (DC_PW, "Administrator", "Creds Range"):
            hits = client.get("/wiki/search", params={"q": q})
            assert hits.status_code == 200 and hits.json() == [], q

    def test_regeneration_keeps_the_notes_and_follows_the_range(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        page = self._page(db_session, rng)
        act_as(world["instructor"])
        notes = "## Notes\n\nThe DC needs a manual reboot after restore.\n"
        body = page.body.split(wiki_doc.END)[0] + wiki_doc.END + "\n\n" + notes
        resp = client.put(f"/wiki/pages/{page.id}", json={"base_revision": page.revision_number, "body": body})
        assert resp.status_code == 200, resp.text
        assert client.put(f"/ranges/{rng.id}", json={"name": "Creds Range v2"}).status_code == 200
        page = self._page(db_session, rng)
        assert page.title == "Creds Range v2" and "# Creds Range v2" in page.body
        assert page.body.rstrip().endswith("The DC needs a manual reboot after restore.")
        assert page.body.count(wiki_doc.BEGIN) == 1 and page.body.count("## Notes") == 1
        revs = client.get(f"/wiki/pages/{page.id}/revisions").json()
        assert revs[0]["edit_summary"] == "Regenerated by TrueNorth"

    def test_a_build_adds_the_addresses_it_recorded(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        rng.state = RangeState.ready
        rng.provisioner_output = json.dumps({"vms": [{"name": f"{str(rng.id)[:8]}-web01", "node_id": "web01",
                                                      "ip": "10.70.0.20", "nics": [{"ip": "10.70.0.20"},
                                                                                    {"ip": "172.16.5.9"}]}]})
        db_session.add(RangeOperation(tenant_id=rng.tenant_id, range_id=rng.id, action="provision", generation=1,
                                      request_hash="x" * 64, status="dispatched", dispatch_attempts=1,
                                      dispatched_at=datetime.now(UTC)))
        db_session.flush()
        ops.reconcile(db_session, rng)
        db_session.commit()
        body = self._page(db_session, rng).body
        assert "10.70.0.20, 172.16.5.9" in body and "10.70.0.10 (planned)" in body

    def test_a_failed_regeneration_never_fails_the_range_change(self, client, db_session, world, monkeypatch,
                                                                caplog):
        rng = _create_range(client, db_session, world)

        def boom(*a, **k):
            raise RuntimeError(f"INSERT ... body={DC_PW}")  # as a database error carries its parameters

        monkeypatch.setattr(wiki_doc, "upsert", boom)
        caplog.set_level(logging.DEBUG)
        act_as(world["instructor"])
        resp = client.put(f"/ranges/{rng.id}", json={"name": "Renamed anyway"})
        assert resp.status_code == 200 and resp.json()["name"] == "Renamed anyway"
        assert "RuntimeError" in caplog.text and DC_PW not in caplog.text  # Low (d)

    def test_the_space_cannot_be_opened_to_everyone_even_when_empty(self, client, db_session, world):
        """M6: the flag decides, not its pages: no page in it, still 409."""
        rng = _create_range(client, db_session, world)
        self._page(db_session, rng).soft_delete()
        db_session.commit()
        act_as(world["admin"])
        for change in ({"visibility": "all"}, {"visibility": "all", "is_archived": False}):
            assert client.put("/wiki/spaces/ranges", json=change).status_code == 409
        assert db_session.query(WikiSpace).filter(WikiSpace.slug == "ranges").one().visibility == "staff"
        assert client.put("/wiki/spaces/ranges", json={"description": "ours"}).status_code == 200

    def test_a_system_space_is_hidden_from_students_whatever_its_column_says(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        page = self._page(db_session, rng)
        db_session.get(WikiSpace, page.space_id).visibility = "all"  # tampered with in the database
        db_session.commit()
        act_as(world["student"])
        assert client.get(f"/wiki/pages/{page.id}").status_code == 404
        assert client.get("/wiki/spaces/ranges").status_code == 404
        assert client.get("/wiki/search", params={"q": "Creds"}).json() == []
        act_as(world["instructor"])
        assert client.put(f"/ranges/{rng.id}", json={"name": "Again"}).status_code == 200
        assert db_session.get(WikiSpace, page.space_id).visibility == "staff"  # put back on the next write

    def test_a_peoples_ranges_space_is_left_alone(self, client, db_session, world):
        """Regeneration never depends on (or writes to) a space people made: the system one
        takes another slug."""
        db_session.add(WikiSpace(tenant_id=world["a"].id, name="Ranges", slug="ranges", visibility="all"))
        db_session.flush()
        rng = _create_range(client, db_session, world)
        page = self._page(db_session, rng)
        space = db_session.get(WikiSpace, page.space_id)
        assert (space.slug, space.system_owned, space.visibility) == ("ranges-generated", True, "staff")


# ── The range designer's save (Low a) ──────────────────────────────────────
_DESIGN = {"cells": [
    {"type": "standard.Rectangle", "id": "z1", "nodeType": "subnet", "position": {"x": 0, "y": 0},
     "size": {"width": 600, "height": 200}, "nodeData": {"label": "LAN", "cidr": "10.9.0.0/24"}},
    {"type": "standard.Rectangle", "id": "a", "nodeType": "dc", "position": {"x": 50, "y": 50},
     "size": {"width": 120, "height": 80}, "nodeData": {"label": "DC01", "os_template": "windows-server-2022"}},
]}  # one node, id dc01
_DC01_ACCOUNT = ("nodes: [{id: dc01, os: windows-server-2022}]\naccounts:\n"
                 "  - {machine: dc01, username: Administrator, password: \"Theirs-Secret-9!\", student_visible: false}\n")


class TestDesignerSave:
    def _range_on(self, db, tmpl, tenant) -> Range:
        rng = Range(name="Designed", template_id=tmpl.id, tenant_id=tenant.id, state=RangeState.created)
        db.add(rng)
        db.commit()
        return rng

    def test_another_tenants_accounts_are_not_copied(self, client, db_session, world):
        theirs = _template(db_session, world["b"].id, account_rules.seal_yaml(_DC01_ACCOUNT), public=True)
        rng = self._range_on(db_session, theirs, world["a"])
        act_as(world["instructor"])
        resp = client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": _DESIGN})
        assert resp.status_code == 200, resp.text
        assert "accounts" not in resp.json()["template"]
        saved = db_session.get(Template, uuid.UUID(resp.json()["template_id"])).yaml
        assert "accounts" not in saved and "tnsec" not in saved

    def test_own_accounts_are_kept_and_must_still_fit(self, client, db_session, world):
        own = _template(db_session, world["a"].id, account_rules.seal_yaml(_DC01_ACCOUNT))
        rng = self._range_on(db_session, own, world["a"])
        act_as(world["instructor"])
        resp = client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": _DESIGN})
        assert resp.status_code == 200, resp.text
        saved = db_session.get(Template, uuid.UUID(resp.json()["template_id"])).yaml
        assert "Theirs-Secret-9!" not in saved and secretbox.PREFIX in saved  # still sealed
        renamed = json.loads(json.dumps(_DESIGN))
        renamed["cells"][1]["nodeData"]["label"] = "WEB01"  # dc01 is gone: its account no longer fits
        resp = client.post(f"/ranges/{rng.id}/topology", json={"diagram_json": renamed})
        assert resp.status_code == 422 and "not a node or asset" in resp.text


# ── Pure parts ──────────────────────────────────────────────────────────
class TestAccountRules:
    def test_machines_resolve_by_id_name_or_asset_role(self):
        doc = {"nodes": [{"id": "dc01", "name": "DC"}], "assets": [{"role": "kali", "type": "vm", "count": 2}],
               "accounts": [{"machine": "DC", "username": "a", "generate": True, "student_visible": False},
                            {"machine": "kali", "username": "kali", "password": "k", "student_visible": True}]}
        specs, errors = account_rules.parse(doc)
        assert errors == [] and [(s.machine, s.generate) for s in specs] == [("dc01", True), ("kali", False)]

    def test_generated_passwords_meet_windows_complexity(self):
        """M3: what Windows checks, for the account it is made for."""
        for _ in range(200):
            pw = account_rules.generate_password("Administrator")
            assert len(pw) == 20 and pw[0].isalnum()
            assert any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw)
            assert not set(pw) & set("'\"\\$` :")
            assert account_rules.windows_password_problem(pw, "Administrator") is None

    def test_a_generated_password_never_contains_the_username(self, monkeypatch):
        picks = iter("abcAbc1-xyz" + "Qq9-wWe5tY7uI8oP0aS1" * 3)
        monkeypatch.setattr(account_rules.secrets, "choice", lambda alphabet: next(picks))
        pw = account_rules.generate_password("abc", length=11)  # the first draw contains it: drawn again
        assert "abc" not in pw.casefold()

    def test_windows_complexity_rules(self):
        rule = account_rules.windows_password_problem
        assert rule("Aa1!aaaa", "Administrator") is None
        assert "8" in rule("Aa1!", "x")
        assert "three of" in rule("abcdefgh1", "x")
        assert "account name" in rule("xxJSMITHx1!", "CORP\\jsmith")
        assert rule("Ab1!Ab1!", "ab") is None  # names under 3 characters are not checked

    def test_notes_preserved_without_the_end_marker(self):
        assert wiki_doc.notes_of("junk\n## Notes\nkeep me\n") == "## Notes\nkeep me"
        assert wiki_doc.notes_of("no heading at all") == wiki_doc.DEFAULT_NOTES
