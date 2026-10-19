"""Range credentials (ADR 0012 §12-14): declared, sealed, role-filtered, audited, documented.

``GET /ranges/{id}/credentials``:

* admin, instructor, range_ops: every account of a range in their tenant;
* a Student: only ``student_visible`` accounts, and only on a range they take part in
  (their own lab session's range, or a running exercise booked to a course they are
  rostered on). Any other range is 404, as is a range with an exercise nobody booked;
* an observer: 403;
* another tenant's range: 404 for everyone.

Rows are written when the range is created and when a build is accepted; generated
passwords are made once and kept; rows are sealed at rest; every read is audited without
the password. The range's wiki page is staff-only and keeps its notes.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _shared import act_as, real_tenant, real_user
from app.course_releases.models import CourseRelease, CourseReleaseBlob
from app.lab_sessions.models import LabSession
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
from app.models_wiki import WikiPage, WikiSpace
from app.range_credentials import RangeCredential, wiki_doc
from app.range_credentials import accounts as account_rules
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


def _lab(db, owner, rng) -> None:
    course = Course(id=uuid.uuid4(), name="C", tenant_id=rng.tenant_id)
    sha = uuid.uuid4().hex * 2
    db.add_all([course, CourseReleaseBlob(sha256=sha, size=1, data=b"x")])
    db.flush()
    rel = CourseRelease(id=uuid.uuid4(), tenant_id=rng.tenant_id, course_id=course.id, catalogue_code="c",
                        arc2_code="a", run_id="r", slug="s", title="T", version=1, release_digest=sha,
                        learner_digest=sha, platform_digest=sha, instructor_digest=sha, blob_sha256=sha, meta="{}")
    db.add(rel)
    db.flush()
    db.add(LabSession(tenant_id=rng.tenant_id, user_id=uuid.UUID(owner.id), release_id=rel.id, activity_id="mod_001",
                      state="ready", profile_id="p", profile_digest="0" * 64, backend="mock", range_id=rng.id))
    db.flush()


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

    def test_an_observer_is_refused(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        act_as(world["observer"])
        assert _creds(client, rng).status_code == 403

    def test_a_student_on_no_part_of_the_range_gets_404(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        act_as(world["student"])
        assert _creds(client, rng).status_code == 404

    def test_a_student_sees_only_the_handed_over_accounts_of_their_own_lab(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        _lab(db_session, world["student"], rng)
        act_as(world["student"])
        resp = _creds(client, rng)
        assert resp.status_code == 200, resp.text
        assert resp.json()["scope"] == "student"
        assert list(_accounts(resp)) == [("web01", "analyst")]
        assert DC_PW not in resp.text
        act_as(world["classmate"])  # not their lab
        assert _creds(client, rng).status_code == 404

    def test_a_rostered_student_of_a_running_booked_exercise(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        _book(db_session, _exercise(db_session, rng), [world["student"]])
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
        _lab(db_session, world["student"], rng)
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

    def test_passwords_in_a_template_are_hidden_from_who_may_not_read_them(self, client, db_session, world):
        own = _template(db_session, world["a"].id)
        public = _template(db_session, world["a"].id, public=True)
        act_as(world["instructor"])
        assert DC_PW in client.get(f"/templates/{own.id}").json()["yaml"]
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

    def test_it_is_written_to_a_staff_only_space_with_the_full_table(self, client, db_session, world):
        rng = _create_range(client, db_session, world)
        page = self._page(db_session, rng)
        space = db_session.get(WikiSpace, page.space_id)
        assert (space.slug, space.visibility, space.tenant_id) == ("ranges", "staff", world["a"].id)
        assert page.title == "Creds Range"
        body = page.body
        assert body.startswith(wiki_doc.BEGIN) and wiki_doc.END in body and "## Notes" in body
        for expected in ("Domain Controller", "windows-server-2022", "10.70.0.10 (planned)", "| lan | 100 |",
                         "creds-lab 2.1", DC_PW, "analyst"):
            assert expected in body, expected
        generated = [a.password for a in db_session.query(RangeCredential).filter(RangeCredential.generated)]
        assert generated and all(pw in body for pw in generated)
        act_as(world["instructor"])
        assert client.get(f"/wiki/pages/{page.id}").status_code == 200

    @pytest.mark.parametrize("who", ["student", "observer"])
    def test_students_and_observers_cannot_read_it_or_find_it(self, client, db_session, world, who):
        rng = _create_range(client, db_session, world)
        _lab(db_session, world["student"], rng)  # even a Student on the range
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

    def test_a_failed_regeneration_never_fails_the_range_change(self, client, db_session, world, monkeypatch):
        rng = _create_range(client, db_session, world)

        def boom(*a, **k):
            raise RuntimeError("wiki unavailable")

        monkeypatch.setattr(wiki_doc, "upsert", boom)
        act_as(world["instructor"])
        resp = client.put(f"/ranges/{rng.id}", json={"name": "Renamed anyway"})
        assert resp.status_code == 200 and resp.json()["name"] == "Renamed anyway"

    def test_the_space_cannot_be_opened_to_everyone(self, client, db_session, world):
        _create_range(client, db_session, world)
        act_as(world["admin"])
        resp = client.put("/wiki/spaces/ranges", json={"visibility": "all"})
        assert resp.status_code == 409
        assert db_session.query(WikiSpace).filter(WikiSpace.slug == "ranges").one().visibility == "staff"

    def test_nothing_is_written_to_a_ranges_space_open_to_all(self, client, db_session, world):
        db_session.add(WikiSpace(tenant_id=world["a"].id, name="Ranges", slug="ranges", visibility="all"))
        db_session.flush()
        rng = _create_range(client, db_session, world)
        assert db_session.query(WikiPage).filter(WikiPage.tags.contains(f"range:{rng.id}")).count() == 0
        assert db_session.query(RangeCredential).filter(RangeCredential.range_id == rng.id).count() == 3


# ── Pure parts ──────────────────────────────────────────────────────────
class TestAccountRules:
    def test_machines_resolve_by_id_name_or_asset_role(self):
        doc = {"nodes": [{"id": "dc01", "name": "DC"}], "assets": [{"role": "kali", "type": "vm", "count": 2}],
               "accounts": [{"machine": "DC", "username": "a", "generate": True, "student_visible": False},
                            {"machine": "kali", "username": "kali", "password": "k", "student_visible": True}]}
        specs, errors = account_rules.parse(doc)
        assert errors == [] and [(s.machine, s.generate) for s in specs] == [("dc01", True), ("kali", False)]

    def test_generated_passwords_meet_windows_complexity(self):
        for _ in range(200):
            pw = account_rules.generate_password()
            assert len(pw) == 20 and pw[0].isalnum()
            assert any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw)
            assert not set(pw) & set("'\"\\$` :")

    def test_notes_preserved_without_the_end_marker(self):
        assert wiki_doc.notes_of("junk\n## Notes\nkeep me\n") == "## Notes\nkeep me"
        assert wiki_doc.notes_of("no heading at all") == wiki_doc.DEFAULT_NOTES
