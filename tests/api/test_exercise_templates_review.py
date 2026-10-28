"""Regression tests from the adversarial review of #342 (ADR 0017).

B1: proven follows the template across edits. M1: a shared item never names another tenant's
private scenario or range template. Plus: bundle format, row locks, size caps, and the
import's consistency check after a conflict relink.
"""

from __future__ import annotations

import uuid

import pytest
from _content_port_kit import (
    DEV,
    acting_as,
    ex_template,
    exercise,
    exercise_template_set,
    made_from,
    prove_scenario,
    prove_template,
    review,
    scenario,
    template,
)
from app.content_port import proven, signing
from app.content_review.models import ContentReviewItem
from app.exercise_templates.models import ExerciseTemplate, ExerciseTemplateUse
from app.models import Range, Scenario, Tenant, UserRole
from test_exercise_templates import payload
from test_exercise_templates_port import exported, item, resign, upload


@pytest.fixture
def port(monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))
    monkeypatch.setenv(signing.TRUSTED_ENV, signing.public_info()["public_key"])


@pytest.fixture
def other(db_session):
    t = Tenant(id=uuid.uuid4(), name="Other org", slug=f"o-{uuid.uuid4().hex[:6]}")
    db_session.add(t)
    db_session.flush()
    return t.id


def status(db, et, scope=None):
    return proven.status(db, "exercise_template", et, scope)


def cond(st, name):
    return next(c for c in st.conditions if c.name == name)


# ── B1: proven follows the template across edits ─────────────────────────────────
class TestProvenAcrossEdits:
    def test_switching_the_range_template_and_roles_unproves_it(self, client, db_session):
        _t, _s, et = exercise_template_set(db_session)
        t2 = prove_template(db_session, template(db_session))  # certified, but no exercise was made from et on it
        assert status(db_session, et, {DEV}).proven
        with acting_as(db_session, UserRole.admin):
            res = client.put(f"/exercise-templates/{et.id}", json={
                "scenario_id": None, "range_template_id": str(t2.id),
                "roles": [{"name": "Whole new", "default_count": 9, "max_participants": 9,
                           "vm_profile": [{"node_type": "ubuntu-2404", "count": 5}]}], "scaling_rules": []})
            assert res.status_code == 200, res.text
        db_session.expire_all()
        st = status(db_session, db_session.get(ExerciseTemplate, et.id), {DEV})
        assert not st.proven and not cond(st, "used").met

    def test_any_edit_needs_a_new_exercise_at_the_new_revision(self, client, db_session):
        _t, _s, et = exercise_template_set(db_session)
        with acting_as(db_session, UserRole.admin):
            assert client.put(f"/exercise-templates/{et.id}", json={"description": "Reworded"}).status_code == 200
        db_session.expire_all()
        et = db_session.get(ExerciseTemplate, et.id)
        assert et.revision == 2 and not cond(status(db_session, et), "used").met
        made_from(db_session, et)  # a new exercise, from revision 2
        assert cond(status(db_session, et), "used").met

    def test_an_exercise_on_a_range_of_another_template_does_not_count(self, db_session):
        t, _s, et = exercise_template_set(db_session)
        t2 = prove_template(db_session, template(db_session))
        ex_t = [u for u in db_session.query(ExerciseTemplateUse).filter_by(template_id=et.id)]
        for use in ex_t:
            db_session.delete(use)
        db_session.flush()
        # same revision, passing score, but the range was built from a different range template
        ex = exercise(db_session, t2)
        db_session.add(ExerciseTemplateUse(exercise_id=ex.id, template_id=et.id, template_revision=et.revision,
                                           tenant_id=DEV))
        db_session.flush()
        assert not cond(status(db_session, et), "used").met
        assert t.id != t2.id

    @pytest.mark.parametrize("change", [
        lambda ids: {"scaling_rules": [{"name": "Tap", "node_type": "ubuntu-2404"}]},
        lambda ids: {"roles": [{"name": "Analyst", "vm_profile": [{"node_type": "ubuntu-2404"}]}]},
        lambda ids: {"scenario_id": None},
        lambda ids: {"range_template_id": ids["t"]},
    ])
    def test_editing_what_it_runs_on_resets_the_review(self, client, db_session, change):
        t, _s, et = exercise_template_set(db_session)
        # a scenario that names the same range template, so the template and scenario agree
        body = change({"t": str(t.id)})
        with acting_as(db_session, UserRole.admin):
            res = client.put(f"/exercise-templates/{et.id}", json=body)
            assert res.status_code == 200, res.text
        db_session.expire_all()
        row = db_session.query(ContentReviewItem).filter_by(item_type="exercise_template", item_id=str(et.id)).one()
        assert row.stale is True
        fresh = proven.freshness(db_session, "exercise_template", str(et.id))
        assert not fresh.met and "Stale" in " ".join(fresh.reasons)

    def test_a_reword_does_not_reset_the_review(self, client, db_session):
        _t, _s, et = exercise_template_set(db_session)
        with acting_as(db_session, UserRole.admin):
            client.put(f"/exercise-templates/{et.id}", json={"instructor_notes": "Different notes"})
        row = db_session.query(ContentReviewItem).filter_by(item_type="exercise_template", item_id=str(et.id)).one()
        assert row.stale is False

    def test_a_review_makes_it_fresh_again(self, client, db_session):
        _t, _s, et = exercise_template_set(db_session)
        with acting_as(db_session, UserRole.admin):
            client.put(f"/exercise-templates/{et.id}", json={"scaling_rules": []})
            row = db_session.query(ContentReviewItem).filter_by(item_type="exercise_template",
                                                                item_id=str(et.id)).one()
            assert client.post(f"/content-review/items/{row.id}/review", json={}).status_code in (200, 201)
        db_session.expire_all()
        assert proven.freshness(db_session, "exercise_template", str(et.id)).met

    def test_an_import_that_changes_roles_resets_the_review(self, client, db_session, port, other):
        _t, _s, et = exercise_template_set(db_session)
        with acting_as(db_session, UserRole.admin):
            upload(client, exported(client, db_session, ("exercise_template", et)), "import", target_tenant_id=other)
            landed = db_session.query(ExerciseTemplate).filter_by(tenant_id=other).one()
            review(db_session, "exercise_template", landed)
            et.roles = [*et.roles, {"name": "Intel", "description": "", "team": "", "min_participants": 0,
                                    "max_participants": 0, "default_count": 0, "vm_profile": []}]
            db_session.flush()
            res = upload(client, exported(client, db_session, ("exercise_template", et)), "import",
                         target_tenant_id=other)
            assert res.status_code == 200 and res.json()["updated"] >= 1
        row = db_session.query(ContentReviewItem).filter_by(item_type="exercise_template",
                                                            item_id=str(landed.id)).one()
        assert row.stale is True


# ── M1: nothing of another tenant's private content is named ─────────────────────
class TestNoLeak:
    def test_a_shared_template_does_not_name_private_dependencies(self, client, db_session, other):
        t, s, et = exercise_template_set(db_session)
        s.name, t.name = "SECRET-PRIVATE-SCENARIO", "SECRET-PRIVATE-RANGE-TEMPLATE"
        et.is_public = True
        db_session.flush()
        with acting_as(db_session, UserRole.admin, other):
            res = client.get(f"/content-port/items/exercise_template/{et.id}/proven")
        assert res.status_code == 200
        assert "SECRET-PRIVATE" not in res.text
        deps = res.json()["dependencies"]
        assert {d["item_type"] for d in deps} == {"range_template", "scenario"}
        assert all(d["proven"] is None for d in deps)

    def test_a_shared_scenario_does_not_name_its_private_range_template(self, client, db_session, other):
        t = prove_template(db_session, template(db_session, name="SECRET-PRIVATE-TEMPLATE"))
        s = prove_scenario(db_session, scenario(db_session, t), t)
        s.is_public = True
        db_session.flush()
        with acting_as(db_session, UserRole.admin, other):
            res = client.get(f"/content-port/items/scenario/{s.id}/proven")
        assert res.status_code == 200 and "SECRET-PRIVATE" not in res.text

    def test_the_owner_still_sees_the_names(self, client, db_session):
        t, s, et = exercise_template_set(db_session)
        with acting_as(db_session, UserRole.admin):
            res = client.get(f"/content-port/items/exercise_template/{et.id}/proven")
        assert {d["title"] for d in res.json()["dependencies"]} == {t.name, s.name}

    def test_the_export_refusal_does_not_name_another_tenants_content(self, client, db_session, port, other):
        t = prove_template(db_session, template(db_session))
        theirs = scenario(db_session, template(db_session, tenant=other), tenant=other, name="SECRET-THEIRS")
        et = ex_template(db_session, theirs, t)
        review(db_session, "exercise_template", et)
        made_from(db_session, et)
        with acting_as(db_session, UserRole.admin):
            res = client.post("/content-port/export",
                              json={"items": [{"item_type": "exercise_template", "item_id": str(et.id)}]})
        assert res.status_code == 409 and "SECRET-THEIRS" not in res.text


# ── the bundle format ─────────────────────────────────────────────────────────────
class TestFormat:
    def test_a_bundle_without_exercise_templates_stays_v1_so_older_sites_read_it(self, client, db_session, port):
        t = prove_template(db_session, template(db_session))
        assert exported(client, db_session, ("range_template", t))["format"] == "truenorth.content-port.v1"

    def test_a_bundle_with_one_is_v2(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        assert exported(client, db_session, ("exercise_template", et))["format"] == "truenorth.content-port.v2"

    def test_v1_cannot_carry_an_exercise_template(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        bad = resign(doc, lambda b: b.update(format="truenorth.content-port.v1"))
        with acting_as(db_session, UserRole.admin):
            res = upload(client, bad)
        assert res.status_code == 422 and "v1" in res.json()["detail"]

    def test_an_unknown_future_format_says_so(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        bad = resign(doc, lambda b: b.update(format="truenorth.content-port.v9"))
        with acting_as(db_session, UserRole.admin):
            res = upload(client, bad)
        assert res.status_code == 422 and "v9" in res.json()["detail"] and "upgrade" in res.json()["detail"].lower()

    def test_a_v2_bundle_still_signs_under_the_same_context(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        assert signing.verify(doc) is not None


# ── locking ───────────────────────────────────────────────────────────────────────
class TestLocking:
    def test_delete_and_create_exercise_lock_the_template_row(self, client, db_session, monkeypatch):
        from sqlalchemy.orm import Query

        locked: list[str] = []
        real = Query.with_for_update

        def spy(self, *a, **k):
            locked.append(str(self.column_descriptions[0]["entity"].__name__))
            return real(self, *a, **k)

        monkeypatch.setattr(Query, "with_for_update", spy)
        t, _s, et = exercise_template_set(db_session)
        et2 = ex_template(db_session, None, t)
        rng = Range(id=uuid.uuid4(), name="r", template_id=t.id, tenant_id=DEV)
        db_session.add(rng)
        db_session.flush()
        with acting_as(db_session, UserRole.admin):
            assert client.post(f"/exercise-templates/{et2.id}/exercises",
                               json={"name": "x", "range_id": str(rng.id)}).status_code == 201
            assert locked.count("ExerciseTemplate") == 1
            et3 = ex_template(db_session, None, t)
            assert client.delete(f"/exercise-templates/{et3.id}").status_code in (204, 409)
            assert locked.count("ExerciseTemplate") == 2


# ── size caps ─────────────────────────────────────────────────────────────────────
class TestCaps:
    def _roles(self, **over):
        role = {"name": "A", "min_participants": 0, "max_participants": 100, "default_count": 10,
                "vm_profile": [{"node_type": "ubuntu-2404", "count": 1}]}
        role.update(over)
        return [role]

    def _post(self, client, db_session, roles):
        with acting_as(db_session, UserRole.admin):
            return client.post("/exercise-templates", json=payload(roles=roles, scaling_rules=[]))

    def test_a_reasonable_template_passes(self, client, db_session):
        assert self._post(client, db_session, self._roles()).status_code == 201

    def test_total_participants_are_capped_at_500(self, client, db_session):
        roles = [{"name": f"R{i}", "max_participants": 200, "default_count": 1} for i in range(3)]
        res = self._post(client, db_session, roles)
        assert res.status_code == 422 and "500" in res.text

    def test_an_unlimited_role_counts_at_its_default(self, client, db_session):
        assert self._post(client, db_session, self._roles(max_participants=0, default_count=400)).status_code == 201
        assert self._post(client, db_session, self._roles(max_participants=0, default_count=501)).status_code == 422

    def test_vms_per_participant_are_capped(self, client, db_session):
        vms = [{"node_type": f"n{i}", "count": 10} for i in range(3)]
        res = self._post(client, db_session, self._roles(vm_profile=vms, max_participants=2, default_count=1))
        assert res.status_code == 422 and "per participant" in res.text

    def test_total_vms_are_capped(self, client, db_session):
        roles = [{"name": f"R{i}", "max_participants": 100, "default_count": 1,
                  "vm_profile": [{"node_type": "x", "count": 10}]} for i in range(3)]
        res = self._post(client, db_session, roles)
        assert res.status_code == 422 and "VMs" in res.text

    def test_the_caps_can_be_set_by_the_operator(self, client, db_session, monkeypatch):
        monkeypatch.setenv("TN_EXERCISE_TEMPLATE_MAX_PARTICIPANTS", "50")
        assert self._post(client, db_session, self._roles(max_participants=60)).status_code == 422
        assert self._post(client, db_session, self._roles(max_participants=40, default_count=10)).status_code == 201

    def test_an_imported_bundle_is_held_to_the_same_caps(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        bad = resign(doc, lambda b: item(b, "exercise_template")["content"]["roles"][0].update(
            max_participants=5000, default_count=4))
        with acting_as(db_session, UserRole.admin):
            res = upload(client, bad)
        assert res.status_code == 422


# ── import: consistency after a conflict relink ──────────────────────────────────
def test_an_import_whose_kept_local_scenario_runs_on_another_template_is_skipped(client, db_session, port, other):
    _t, _s, et = exercise_template_set(db_session)
    with acting_as(db_session, UserRole.admin):
        assert upload(client, exported(client, db_session, ("exercise_template", et)), "import",
                      target_tenant_id=other).status_code == 200
    landed = db_session.query(ExerciseTemplate).filter_by(tenant_id=other).one()
    local_s = db_session.get(Scenario, landed.scenario_id)
    elsewhere = template(db_session, tenant=other)
    local_s.yaml = f"name: Port scenario\nrange_template: {elsewhere.id}\nobjectives: []\n"  # edited here
    et.description = "Origin edited it"
    db_session.flush()
    with acting_as(db_session, UserRole.admin):
        res = upload(client, exported(client, db_session, ("exercise_template", et)), "import",
                     target_tenant_id=other)  # the scenario conflict is not confirmed: the local scenario stays
    assert res.status_code == 200, res.text
    out = {i["item_type"]: i for i in res.json()["items"]}
    assert out["scenario"]["action"] == "skipped"
    assert out["exercise_template"]["action"] == "skipped"
    assert "range template" in " ".join(out["exercise_template"]["reasons"])
    db_session.refresh(landed)
    assert landed.description != "Origin edited it"


def test_an_unconfirmed_name_clash_leaves_no_template_behind(client, db_session, port, other):
    _t, s, et = exercise_template_set(db_session)
    scenario(db_session, template(db_session, tenant=other), tenant=other, name=s.name)
    with acting_as(db_session, UserRole.admin):
        res = upload(client, exported(client, db_session, ("exercise_template", et)), "import",
                     target_tenant_id=other)
    out = {i["item_type"]: i for i in res.json()["items"]}
    assert out["scenario"]["action"] == "skipped" and out["exercise_template"]["action"] == "skipped"
    assert db_session.query(ExerciseTemplate).filter_by(tenant_id=other).count() == 0
