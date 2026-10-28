"""The exercise template record (ADR 0016; app/exercise_templates).

A reusable exercise: objectives, MESL, scenario, range template, teams, instructor notes. A
concrete exercise is created from it and keeps a reference to the template and the revision
it came from; editing the template never changes exercises already made from it. Tenant-scoped
unless a platform administrator shares it. Everything here is staff-only (exercise:create),
because the MESL and the notes are the exercise-control plan.
"""

from __future__ import annotations

import uuid

import pytest
from _content_port_kit import (
    DEV,
    acting_as,
    ex_template,
    made_from,
    scenario,
    template,
)
from app.exercise_templates.models import ExerciseTemplate, ExerciseTemplateUse
from app.models import AuditLog, Exercise, ExerciseObjective, ExerciseState, MeslEvent, Range, Tenant, UserRole


@pytest.fixture
def port(monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))


@pytest.fixture
def other(db_session):
    t = Tenant(id=uuid.uuid4(), name="Other org", slug=f"other-{uuid.uuid4().hex[:6]}")
    db_session.add(t)
    db_session.flush()
    return t.id


def payload(**over) -> dict:
    body = {
        "name": "IR drill", "description": "Respond to the intrusion.",
        "teams": [{"name": "Blue", "side": "blue", "min_students": 2, "max_students": 6},
                  {"name": "White cell", "side": "white"}],
        "objectives": [{"ref": "O1", "text": "Detect the phish", "moe": "within 15 minutes"},
                       {"ref": "O2", "text": "Contain the host"}],
        "mesl": [{"serial": 1, "phase": "D1", "scenario_time": "D1 0900", "title": "Phish lands",
                  "objective_ref": "O1", "delivery_method": "email", "from_cell": "OPFOR", "to_participant": "Blue",
                  "expected_action": "Report it"},
                 {"serial": 2, "phase": "D1", "scenario_time": "D1 1000", "title": "Beacon",
                  "delivery_method": "cyber"}],
        "roles": [{"name": "Analyst", "description": "Triage alerts", "team": "Blue", "min_participants": 2,
                   "max_participants": 12, "default_count": 4,
                   "vm_profile": [{"node_type": "ubuntu-2404", "count": 1}, {"node_type": "siem-console"}]},
                  {"name": "Red", "min_participants": 1, "default_count": 1,
                   "vm_profile": [{"node_type": "kali"}]}],
        "scaling_rules": [{"name": "Sensor", "node_type": "zeek", "kind": "per_segment"},
                          {"name": "Targets", "node_type": "srv2022", "kind": "per_participants", "count": 2,
                           "per": 5, "role": "Red"},
                          {"name": "Domain controller", "node_type": "srv2022", "kind": "fixed"}],
        "instructor_notes": "Hold serial 2 until Blue has reported.", "tags": ["DFIR"],
    }
    body.update(over)
    return body


def make_range(db, tmpl, tenant=DEV) -> Range:
    r = Range(id=uuid.uuid4(), name=f"r-{uuid.uuid4().hex[:6]}", template_id=tmpl.id, tenant_id=tenant)
    db.add(r)
    db.flush()
    return r


# ── permissions ──────────────────────────────────────────────────────────────────
class TestPermissions:
    @pytest.mark.parametrize("role", [UserRole.admin, UserRole.instructor])
    def test_staff_who_author_exercises_can_use_templates(self, client, db_session, role):
        with acting_as(db_session, role):
            res = client.post("/exercise-templates", json=payload())
            assert res.status_code == 201, res.text
            tid = res.json()["id"]
            assert client.get("/exercise-templates").status_code == 200
            assert client.get(f"/exercise-templates/{tid}").status_code == 200
            assert client.put(f"/exercise-templates/{tid}", json={"name": "Renamed"}).status_code == 200
            assert client.delete(f"/exercise-templates/{tid}").status_code == 204

    @pytest.mark.parametrize("role", [UserRole.range_ops, UserRole.observer, UserRole.student])
    def test_everyone_else_is_refused_everything(self, client, db_session, role):
        et = ex_template(db_session)
        rng = make_range(db_session, template(db_session))
        mine = str(et.id)
        with acting_as(db_session, role):
            assert client.get("/exercise-templates").status_code == 403
            assert client.post("/exercise-templates", json=payload()).status_code == 403
            assert client.get(f"/exercise-templates/{mine}").status_code == 403
            assert client.put(f"/exercise-templates/{mine}", json={"name": "x"}).status_code == 403
            assert client.delete(f"/exercise-templates/{mine}").status_code == 403
            assert client.post(f"/exercise-templates/{mine}/exercises",
                               json={"name": "x", "range_id": str(rng.id)}).status_code == 403
            assert client.get(f"/exercise-templates/{mine}/exercises").status_code == 403
            assert client.post(f"/exercise-templates/from-exercise/{uuid.uuid4()}",
                               json={"name": "x"}).status_code == 403

    def test_only_the_platform_admin_shares(self, client, db_session, port, other):
        with acting_as(db_session, UserRole.instructor):
            assert client.post("/exercise-templates", json=payload(is_public=True)).status_code == 403
            tid = client.post("/exercise-templates", json=payload()).json()["id"]
            assert client.put(f"/exercise-templates/{tid}", json={"is_public": True}).status_code == 403
        with acting_as(db_session, UserRole.admin, other):  # a tenant admin is not the platform admin
            assert client.post("/exercise-templates", json=payload(is_public=True)).status_code == 403
        with acting_as(db_session, UserRole.admin):
            res = client.post("/exercise-templates", json=payload(is_public=True))
            assert res.status_code == 201 and res.json()["is_public"] is True


# ── tenancy ──────────────────────────────────────────────────────────────────────
class TestTenancy:
    def test_another_tenants_private_template_is_not_found(self, client, db_session, other):
        et = ex_template(db_session, tenant=other)
        rng = make_range(db_session, template(db_session))
        with acting_as(db_session, UserRole.admin):
            assert client.get("/exercise-templates").json() == []
            for res in (client.get(f"/exercise-templates/{et.id}"),
                        client.put(f"/exercise-templates/{et.id}", json={"name": "x"}),
                        client.delete(f"/exercise-templates/{et.id}"),
                        client.get(f"/exercise-templates/{et.id}/exercises"),
                        client.post(f"/exercise-templates/{et.id}/exercises",
                                    json={"name": "x", "range_id": str(rng.id)})):
                assert res.status_code == 404, res.text
        assert db_session.get(ExerciseTemplate, et.id).name == et.name

    def test_a_shared_template_is_listed_and_read_but_not_changed_by_other_tenants(self, client, db_session, other):
        et = ex_template(db_session, tenant=other, public=True)
        with acting_as(db_session, UserRole.instructor):
            listed = client.get("/exercise-templates").json()
            assert [(r["id"], r["shared"]) for r in listed] == [(str(et.id), True)]
            assert client.get(f"/exercise-templates/{et.id}").status_code == 200
            assert client.put(f"/exercise-templates/{et.id}", json={"name": "x"}).status_code == 404
            assert client.delete(f"/exercise-templates/{et.id}").status_code == 404

    def test_a_template_cannot_name_another_tenants_scenario_or_range_template(self, client, db_session, other):
        theirs_t = template(db_session, tenant=other)
        theirs_s = scenario(db_session, theirs_t, tenant=other)
        with acting_as(db_session, UserRole.admin):
            assert client.post("/exercise-templates", json=payload(scenario_id=str(theirs_s.id))).status_code == 422
            assert client.post("/exercise-templates",
                               json=payload(range_template_id=str(theirs_t.id))).status_code == 422
            res = client.post("/exercise-templates", json=payload())
            tid = res.json()["id"]
            assert client.put(f"/exercise-templates/{tid}",
                              json={"scenario_id": str(theirs_s.id)}).status_code == 422

    def test_a_public_scenario_and_range_template_may_be_named(self, client, db_session, other):
        t = template(db_session, tenant=other, public=True)
        s = scenario(db_session, t, tenant=other)
        s.is_public = True
        db_session.flush()
        with acting_as(db_session, UserRole.admin):
            res = client.post("/exercise-templates", json=payload(scenario_id=str(s.id), range_template_id=str(t.id)))
            assert res.status_code == 201, res.text


# ── the record ───────────────────────────────────────────────────────────────────
class TestRecord:
    def test_create_round_trips_and_is_audited(self, client, db_session):
        t = template(db_session)
        s = scenario(db_session, t)
        with acting_as(db_session, UserRole.instructor) as who:
            res = client.post("/exercise-templates", json=payload(scenario_id=str(s.id), range_template_id=str(t.id)))
            assert res.status_code == 201, res.text
            body = res.json()
            again = client.get(f"/exercise-templates/{body['id']}").json()
        assert again == body
        assert body["revision"] == 1 and body["tenant_id"] == str(DEV) and body["is_public"] is False
        assert body["scenario_id"] == str(s.id) and body["range_template_id"] == str(t.id)
        assert [o["ref"] for o in body["objectives"]] == ["O1", "O2"]
        assert [m["serial"] for m in body["mesl"]] == [1, 2] and body["teams"][0]["side"] == "blue"
        assert body["instructor_notes"].startswith("Hold serial 2") and body["tags"] == ["DFIR"]
        row = db_session.get(ExerciseTemplate, uuid.UUID(body["id"]))
        assert row.created_by == who.id
        assert db_session.query(AuditLog).filter_by(action="create", resource_type="exercise_template",
                                                    resource_id=body["id"]).count() == 1

    def test_the_list_is_summaries_without_the_answer_key(self, client, db_session):
        ex_template(db_session)
        with acting_as(db_session, UserRole.admin):
            row = client.get("/exercise-templates").json()[0]
        assert row["objectives"] == 2 and row["mesl_events"] == 3 and "instructor_notes" not in row
        assert "mesl" not in row

    def test_update_changes_the_revision_and_deleting_hides_it(self, client, db_session):
        et = ex_template(db_session)
        with acting_as(db_session, UserRole.admin):
            res = client.put(f"/exercise-templates/{et.id}", json={"description": "New text", "tags": ["IR"]})
            assert res.status_code == 200 and res.json()["revision"] == 2
            assert res.json()["objectives"] and res.json()["instructor_notes"]  # untouched fields stay
            assert client.delete(f"/exercise-templates/{et.id}").status_code == 204
            assert client.get(f"/exercise-templates/{et.id}").status_code == 404
            assert client.get("/exercise-templates").json() == []
        assert db_session.get(ExerciseTemplate, et.id).deleted_at is not None

    def test_a_template_exercises_were_made_from_is_kept_not_deleted(self, client, db_session):
        """ADR 0010: what history depends on is refused (409); the exercises and their evidence stay."""
        t = template(db_session)
        et = ex_template(db_session, scenario(db_session, t), t)
        ex = made_from(db_session, et)
        with acting_as(db_session, UserRole.admin):
            res = client.delete(f"/exercise-templates/{et.id}")
            assert res.status_code == 409 and "1 exercise" in res.json()["detail"]
            assert client.get(f"/exercise-templates/{et.id}").status_code == 200
        assert db_session.get(ExerciseTemplate, et.id).deleted_at is None
        assert db_session.get(ExerciseTemplateUse, ex.id).template_id == et.id

    @pytest.mark.parametrize("change", [
        {"kind": "assessment"},  # exercises are collective: there is no individual shape to choose
        {"objectives": [{"ref": "O1", "text": "a"}, {"ref": "O1", "text": "b"}]},
        {"mesl": [{"serial": 1, "title": "a", "objective_ref": "Z9"}]},
        {"mesl": [{"serial": 1, "title": "a"}, {"serial": 1, "title": "b"}]},
        {"mesl": [{"serial": 1, "title": "a", "delivery_method": "carrier pigeon"}]},
        {"teams": [{"name": "Blue", "side": "blue"}, {"name": "Blue", "side": "red"}]},
        {"teams": [{"name": "Blue", "side": "purple"}]},
        {"teams": [{"name": "Blue", "side": "blue", "min_students": 5, "max_students": 2}]},
        {"name": ""}, {"name": "a\nb"}, {"name": "a\x00b"}, {"kind": "weird"},
        {"objectives": [{"ref": "O1", "text": "x", "surprise": 1}]},
        {"tags": ["x"] * 40},
        {"roles": [{"name": "A"}, {"name": "A"}]},
        {"roles": [{"name": "A", "min_participants": 5, "max_participants": 2}]},
        {"roles": [{"name": "A", "min_participants": 2, "max_participants": 6, "default_count": 9}]},
        {"roles": [{"name": "A", "default_count": 0, "min_participants": 3}]},
        {"roles": [{"name": "A", "team": "Nobody"}]},
        {"roles": [{"name": "A", "vm_profile": [{"node_type": "x"}, {"node_type": "x"}]}]},
        {"roles": [{"name": "A", "vm_profile": [{"node_type": "x", "count": 0}]}]},
        {"roles": [{"name": "A", "vm_profile": [{"node_type": "bad\nline"}]}]},
        {"roles": [{"name": "A", "surprise": 1}]},
        {"scaling_rules": [{"name": "R", "node_type": "x", "kind": "per_participants"}]},  # needs per
        {"scaling_rules": [{"name": "R", "node_type": "x", "kind": "per_participants", "per": 3, "role": "Nope"}]},
        {"scaling_rules": [{"name": "R", "node_type": "x", "kind": "weird"}]},
        {"scaling_rules": [{"name": "R", "node_type": "x"}, {"name": "R", "node_type": "y"}]},
        {"scaling_rules": [{"name": "R", "node_type": "x", "kind": "fixed", "per": 3}]},  # per is for steps only
    ])
    def test_a_malformed_template_is_refused(self, client, db_session, change):
        with acting_as(db_session, UserRole.admin):
            assert client.post("/exercise-templates", json=payload(**change)).status_code == 422

    def test_roles_and_sizing_rules_round_trip_and_are_replaced_whole(self, client, db_session):
        with acting_as(db_session, UserRole.admin):
            body = client.post("/exercise-templates", json=payload()).json()
            tid = body["id"]
            assert [r["name"] for r in body["roles"]] == ["Analyst", "Red"]
            analyst = body["roles"][0]
            assert (analyst["team"], analyst["min_participants"], analyst["max_participants"],
                    analyst["default_count"]) == ("Blue", 2, 12, 4)
            assert analyst["vm_profile"] == [{"node_type": "ubuntu-2404", "count": 1},
                                             {"node_type": "siem-console", "count": 1}]
            assert [(r["kind"], r["count"], r["per"], r["role"]) for r in body["scaling_rules"]] == [
                ("per_segment", 1, 0, ""), ("per_participants", 2, 5, "Red"), ("fixed", 1, 0, "")]
            assert client.get("/exercise-templates").json()[0]["role_names"] == ["Analyst", "Red"]
            kept = client.put(f"/exercise-templates/{tid}", json={"description": "x"}).json()
            assert kept["roles"] == body["roles"] and kept["scaling_rules"] == body["scaling_rules"]
            cleared = client.put(f"/exercise-templates/{tid}", json={"roles": [], "scaling_rules": []}).json()
            assert cleared["roles"] == [] and cleared["scaling_rules"] == []
            # a rule that names a role, with the role gone, is refused
            again = client.put(f"/exercise-templates/{tid}", json={
                "roles": [], "scaling_rules": [{"name": "R", "node_type": "x", "kind": "per_participants",
                                                "per": 2, "role": "Red"}]})
            assert again.status_code == 422

    def test_removing_a_role_a_rule_names_is_refused(self, client, db_session):
        with acting_as(db_session, UserRole.admin):
            tid = client.post("/exercise-templates", json=payload()).json()["id"]
            assert client.put(f"/exercise-templates/{tid}", json={"roles": []}).status_code == 422

    def test_a_template_is_collective_and_has_no_kind_field(self, client, db_session):
        with acting_as(db_session, UserRole.admin):
            res = client.post("/exercise-templates", json=payload())
            assert res.status_code == 201 and "kind" not in res.json()
            assert client.post("/exercise-templates", json=payload(teams=[], roles=[], scaling_rules=[], mesl=[])).status_code == 201

    def test_the_scenario_and_the_range_template_must_agree(self, client, db_session):
        t1, t2 = template(db_session), template(db_session)
        s = scenario(db_session, t1)
        with acting_as(db_session, UserRole.admin):
            bad = client.post("/exercise-templates", json=payload(scenario_id=str(s.id), range_template_id=str(t2.id)))
            assert bad.status_code == 422 and "range template" in bad.text
            assert client.post("/exercise-templates",
                               json=payload(scenario_id=str(s.id), range_template_id=str(t1.id))).status_code == 201


# ── creating an exercise from a template ─────────────────────────────────────────
class TestCreateExercise:
    def _full(self, db):
        t = template(db)
        s = scenario(db, t)
        return t, s, ex_template(db, s, t)

    def test_it_copies_objectives_and_mesl_and_remembers_the_template_and_revision(self, client, db_session):
        t, s, et = self._full(db_session)
        rng = make_range(db_session, t)
        with acting_as(db_session, UserRole.instructor):
            res = client.post(f"/exercise-templates/{et.id}/exercises", json={"name": "Drill 1", "range_id": str(rng.id)})
            assert res.status_code == 201, res.text
            body = res.json()
        ex = db_session.get(Exercise, uuid.UUID(body["id"]))
        assert (ex.name, ex.kind, ex.range_id, ex.scenario_id, ex.tenant_id, ex.state) == (
            "Drill 1", "collective", rng.id, s.id, DEV, ExerciseState.pending)
        objs = db_session.query(ExerciseObjective).filter_by(exercise_id=ex.id).order_by(ExerciseObjective.ordinal).all()
        assert [(o.ref, o.text, o.moe, o.competency_code) for o in objs] == [
            ("O1", "Detect the phish", "within 15 minutes", "DCWF-1"), ("O2", "Contain the host", "", "")]
        mesl = db_session.query(MeslEvent).filter_by(exercise_id=ex.id).order_by(MeslEvent.serial).all()
        assert [(m.serial, m.status, m.objective_ref, m.delivery_method) for m in mesl] == [
            (1, "planned", "O1", "email"), (2, "planned", "", "white_cell"), (3, "planned", "", "white_cell")]
        use = db_session.get(ExerciseTemplateUse, ex.id)
        assert (use.template_id, use.template_revision, use.tenant_id) == (et.id, 1, DEV)
        assert body["template_id"] == str(et.id) and body["template_revision"] == 1
        assert body["objectives"] == 2 and body["mesl_events"] == 3
        assert db_session.query(AuditLog).filter_by(action="create_from_template", resource_type="exercise",
                                                    resource_id=str(ex.id)).count() == 1

    def test_editing_the_template_never_changes_exercises_already_made(self, client, db_session):
        t, _s, et = self._full(db_session)
        rng = make_range(db_session, t)
        with acting_as(db_session, UserRole.admin):
            ex_id = client.post(f"/exercise-templates/{et.id}/exercises",
                                json={"name": "Drill 1", "range_id": str(rng.id)}).json()["id"]
            res = client.put(f"/exercise-templates/{et.id}", json={
                "objectives": [{"ref": "N1", "text": "Something new"}], "mesl": [{"serial": 9, "title": "New"}]})
            assert res.status_code == 200 and res.json()["revision"] == 2
            second = client.post(f"/exercise-templates/{et.id}/exercises",
                                 json={"name": "Drill 2", "range_id": str(rng.id)}).json()
            listed = client.get(f"/exercise-templates/{et.id}/exercises").json()
        first_objs = db_session.query(ExerciseObjective).filter_by(exercise_id=uuid.UUID(ex_id)).count()
        assert first_objs == 2
        assert [m.serial for m in db_session.query(MeslEvent).filter_by(exercise_id=uuid.UUID(ex_id))
                .order_by(MeslEvent.serial)] == [1, 2, 3]
        assert db_session.get(ExerciseTemplateUse, uuid.UUID(ex_id)).template_revision == 1
        assert second["template_revision"] == 2
        assert {(r["name"], r["template_revision"]) for r in listed} == {("Drill 1", 1), ("Drill 2", 2)}

    def test_a_template_without_a_scenario_still_makes_a_collective_exercise(self, client, db_session):
        t = template(db_session)
        rng = make_range(db_session, t)
        bare = ex_template(db_session, None, t)
        with acting_as(db_session, UserRole.admin):
            ok = client.post(f"/exercise-templates/{bare.id}/exercises",
                             json={"name": "Solo", "range_id": str(rng.id), "max_score": 50})
            assert ok.status_code == 201, ok.text
        ex = db_session.get(Exercise, uuid.UUID(ok.json()["id"]))
        assert ex.kind == "collective" and ex.scenario_id is None and ex.max_score == 50

    def test_the_range_must_be_this_tenants_and_built_from_the_templates_range_template(self, client, db_session,
                                                                                      other):
        t, _s, et = self._full(db_session)
        theirs = make_range(db_session, template(db_session, tenant=other), tenant=other)
        elsewhere = make_range(db_session, template(db_session))
        with acting_as(db_session, UserRole.admin):
            for rid in (theirs.id, elsewhere.id, uuid.uuid4()):
                res = client.post(f"/exercise-templates/{et.id}/exercises", json={"name": "x", "range_id": str(rid)})
                assert res.status_code == 422, (rid, res.text)
        assert db_session.query(Exercise).filter(Exercise.name == "x").count() == 0

    def test_a_shared_template_whose_scenario_is_another_tenants_is_refused_with_a_reason(self, client, db_session,
                                                                                        other):
        t = template(db_session, tenant=other, public=True)
        s = scenario(db_session, t, tenant=other)
        s.is_public = True
        et = ex_template(db_session, s, t, tenant=other, public=True)
        mine = template(db_session)
        et.range_template_id = None  # nothing to match the range against
        rng = make_range(db_session, mine)
        db_session.flush()
        with acting_as(db_session, UserRole.admin):
            res = client.post(f"/exercise-templates/{et.id}/exercises", json={"name": "x", "range_id": str(rng.id)})
        assert res.status_code == 422 and "scenario" in res.text

    def test_the_new_exercise_works_like_any_other(self, client, db_session):
        t, _s, et = self._full(db_session)
        rng = make_range(db_session, t)
        with acting_as(db_session, UserRole.admin):
            ex_id = client.post(f"/exercise-templates/{et.id}/exercises",
                                json={"name": "Drill 1", "range_id": str(rng.id)}).json()["id"]
            got = client.get(f"/collective-exercises/{ex_id}").json()
            assert [o["ref"] for o in got["objectives"]] == ["O1", "O2"] and len(got["mesl"]) == 3
            assert client.get(f"/exercises/{ex_id}").status_code == 200


# ── saving an existing exercise as a template ────────────────────────────────────
class TestSaveAsTemplate:
    def test_a_collective_exercise_becomes_a_template(self, client, db_session):
        t = template(db_session)
        s = scenario(db_session, t)
        rng = make_range(db_session, t)
        ex = Exercise(id=uuid.uuid4(), name="Old drill", kind="collective", range_id=rng.id, scenario_id=s.id,
                      tenant_id=DEV)
        db_session.add(ex)
        db_session.flush()
        db_session.add(ExerciseObjective(exercise_id=ex.id, ref="E1", text="Contain", moe="fast", ordinal=0))
        db_session.add(MeslEvent(exercise_id=ex.id, serial=1, title="Start", status="delivered", objective_ref="E1",
                                 delivery_method="radio"))
        db_session.flush()
        with acting_as(db_session, UserRole.instructor):
            res = client.post(f"/exercise-templates/from-exercise/{ex.id}", json={"name": "Drill template"})
            assert res.status_code == 201, res.text
        body = res.json()
        assert (body["name"], body["scenario_id"], body["range_template_id"], body["revision"]) == (
            "Drill template", str(s.id), str(t.id), 1)
        assert body["objectives"][0]["ref"] == "E1" and body["mesl"][0]["title"] == "Start"
        assert body["is_public"] is False and body["tenant_id"] == str(DEV)
        assert db_session.get(Exercise, ex.id).state == ExerciseState.pending  # the exercise is untouched
        assert db_session.get(ExerciseTemplateUse, ex.id) is None  # it was not made from the template

    def test_an_individual_exercise_cannot_be_saved(self, client, db_session):
        t = template(db_session)
        rng = make_range(db_session, t)
        ex = Exercise(id=uuid.uuid4(), name="Practical", kind="assessment", range_id=rng.id, tenant_id=DEV)
        db_session.add(ex)
        db_session.flush()
        with acting_as(db_session, UserRole.admin):
            res = client.post(f"/exercise-templates/from-exercise/{ex.id}", json={"name": "x"})
        assert res.status_code == 422 and "collective" in res.text

    def test_another_tenants_exercise_is_not_found(self, client, db_session, other):
        t = template(db_session, tenant=other)
        rng = make_range(db_session, t, tenant=other)
        ex = Exercise(id=uuid.uuid4(), name="Theirs", kind="collective", range_id=rng.id, tenant_id=other)
        db_session.add(ex)
        db_session.flush()
        with acting_as(db_session, UserRole.admin):
            assert client.post(f"/exercise-templates/from-exercise/{ex.id}", json={"name": "x"}).status_code == 404
        assert db_session.query(ExerciseTemplate).count() == 0
