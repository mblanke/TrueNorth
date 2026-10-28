"""An exercise template is proven, and ported, like the content it builds on (ADR 0017).

Proven: a range built from its range template reached Certified, an exercise MADE FROM THE
TEMPLATE completed with a passing score, and the content clock does not call it stale.
Port: export carries it with its scenario and range template (which must be proven too);
import lands it not yet proven, relinked to the local copies, under the content port's
signature, preview, conflict and replay rules (docs/content-port.md).
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime

import pytest
from _content_port_kit import (
    DEV,
    acting_as,
    ex_template,
    exercise,
    exercise_template_set,
    made_from,
    prove_exercise_template,
    prove_scenario,
    prove_template,
    review,
    scenario,
    template,
)
from _content_port_kit import user as make_user
from app.content_feeds import bundle_signing as feed_signing
from app.content_port import ContentPortImport, ContentPortItem, proven, signing
from app.content_port import bundle as bnd
from app.exercise_templates.models import ExerciseTemplate, ExerciseTemplateUse
from app.models import AuditLog, ExerciseState, Scenario, Template, Tenant, UserRole


@pytest.fixture
def port(monkeypatch):
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV))
    monkeypatch.setenv(signing.TRUSTED_ENV, signing.public_info()["public_key"])


@pytest.fixture
def new_site(db_session):
    t = Tenant(id=uuid.uuid4(), name="New site", slug=f"new-{uuid.uuid4().hex[:6]}")
    db_session.add(t)
    db_session.flush()
    return t.id


def export(client, *refs):
    return client.post("/content-port/export",
                       json={"items": [{"item_type": t, "item_id": str(r.id)} for t, r in refs]})


def upload(client, doc, path="preview", **form):
    raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    data = {k: (str(v) if not isinstance(v, list) else v) for k, v in form.items()}
    return client.post(f"/content-port/{path}", files={"file": ("bundle.json", raw, "application/json")}, data=data)


def resign(doc: dict, mutate=None) -> dict:
    body = copy.deepcopy({k: v for k, v in doc.items() if k != "signature"})
    if mutate:
        mutate(body)
    for i in body.get("items", []):
        if isinstance(i, dict) and isinstance(i.get("content"), dict):
            i["content_sha256"] = bnd.sha256(i["content"])
    return feed_signing.sign_for(signing.CONTEXT, body, signing._signing_key())


def exported(client, db_session, *refs) -> dict:
    with acting_as(db_session, UserRole.admin):
        res = export(client, *refs)
    assert res.status_code == 200, res.text
    return res.json()


def item(doc, item_type):
    return next(i for i in doc["items"] if i["item_type"] == item_type)


def status(db, et, scope=None):
    return proven.status(db, "exercise_template", et, scope)


def cond(st, name):
    return next(c for c in st.conditions if c.name == name)


# ── proven ───────────────────────────────────────────────────────────────────────
class TestProven:
    def test_all_three_conditions_make_it_proven(self, db_session):
        t, s, et = exercise_template_set(db_session)
        st = status(db_session, et)
        assert st.proven and st.reasons == []
        assert {(d.item_type, d.item_id) for d in st.dependencies} == {("range_template", str(t.id)),
                                                                        ("scenario", str(s.id))}
        assert len(cond(st, "certified").evidence) >= 1
        assert [e.kind for e in cond(st, "used").evidence] == ["exercise"]
        assert st.golden_builds == [{"catalogue_id": "ubuntu-2404", "build_id": "b-2026-10-01", "checksum": "a" * 64}]

    def test_an_exercise_not_made_from_it_does_not_count(self, db_session):
        t = prove_template(db_session, template(db_session))
        s = prove_scenario(db_session, scenario(db_session, t), t)
        et = ex_template(db_session, s, t)
        review(db_session, "exercise_template", et)
        exercise(db_session, t, s)  # a completed passing exercise on the same scenario, but not from the template
        st = status(db_session, et)
        assert not st.proven and "No exercise made from it" in " ".join(st.reasons)

    @pytest.mark.parametrize("score,state", [((50, 100), ExerciseState.completed), ((90, 100), ExerciseState.running),
                                            ((0, 0), ExerciseState.completed)])
    def test_a_failed_or_unfinished_exercise_does_not_count(self, db_session, score, state):
        t = prove_template(db_session, template(db_session))
        et = ex_template(db_session, None, t)
        review(db_session, "exercise_template", et)
        made_from(db_session, et, score=score, state=state)
        assert not cond(status(db_session, et), "used").met

    def test_no_certified_range_template(self, db_session):
        t = template(db_session)
        exercise(db_session, t)
        review(db_session, "range_template", t)
        et = prove_exercise_template(db_session, ex_template(db_session, None, t))
        st = status(db_session, et)
        assert not st.proven and "No range built from it has reached Certified" in " ".join(st.reasons)

    def test_a_range_template_edited_since_it_was_certified_unproves_it(self, db_session):
        t, _s, et = exercise_template_set(db_session)
        assert status(db_session, et).proven
        t.yaml = t.yaml + "# changed\n"
        db_session.flush()
        st = status(db_session, et)
        assert not st.proven and "earlier version" in " ".join(st.reasons)

    def test_a_role_or_rule_naming_a_node_the_range_template_lacks_is_not_proven(self, db_session):
        _t, _s, et = exercise_template_set(db_session)
        assert status(db_session, et).proven  # ids, types and os values of its nodes all resolve
        et.roles = [*et.roles, {"name": "Intel", "description": "", "team": "", "min_participants": 0,
                                "max_participants": 0, "default_count": 0,
                                "vm_profile": [{"node_type": "intel-workstation", "count": 1}]}]
        et.scaling_rules = [*et.scaling_rules, {"name": "Tap", "node_type": "zeek", "kind": "fixed", "count": 1,
                                                "per": 0, "role": "", "segment": ""}]
        db_session.flush()
        st = status(db_session, et)
        assert not st.proven and not cond(st, "certified").met
        text = " ".join(st.reasons)
        assert "Role Intel needs node type 'intel-workstation'" in text and "Scaling rule Tap needs node type 'zeek'" in text

    def test_a_node_may_be_named_by_its_id(self, db_session):
        _t, _s, et = exercise_template_set(db_session)
        et.roles = [{"name": "Analyst", "description": "", "team": "", "min_participants": 0, "max_participants": 0,
                     "default_count": 0, "vm_profile": [{"node_type": "ws01", "count": 2}]}]
        db_session.flush()
        assert status(db_session, et).proven

    def test_it_must_name_a_range_template(self, db_session):
        et = ex_template(db_session)
        review(db_session, "exercise_template", et)
        ex = exercise(db_session, prove_template(db_session, template(db_session)))
        db_session.add(ExerciseTemplateUse(exercise_id=ex.id, template_id=et.id, template_revision=1, tenant_id=DEV))
        db_session.flush()
        st = status(db_session, et)
        assert not st.proven and "names no range template" in " ".join(st.reasons)

    def test_a_stale_or_untracked_template_is_not_proven(self, db_session):
        t = prove_template(db_session, template(db_session))
        et = ex_template(db_session, None, t)
        made_from(db_session, et)
        assert "Not on the content clock" in " ".join(status(db_session, et).reasons)
        review(db_session, "exercise_template", et, stale=True)
        assert "Stale" in " ".join(status(db_session, et).reasons)

    def test_a_scenario_it_cannot_read_leaves_it_not_proven(self, db_session):
        t, s, et = exercise_template_set(db_session)
        s.deleted_at = datetime.now(UTC)
        db_session.flush()
        st = status(db_session, et)
        assert not st.proven and "missing here, deleted, or not shared" in " ".join(st.reasons)

    def test_only_the_callers_tenants_evidence_counts(self, db_session):
        t = prove_template(db_session, template(db_session, public=True))
        et = ex_template(db_session, None, t, public=True)
        review(db_session, "exercise_template", et)
        other = Tenant(id=uuid.uuid4(), name="Other", slug=f"o-{uuid.uuid4().hex[:6]}")
        db_session.add(other)
        db_session.flush()
        made_from(db_session, et, tenant=other.id)
        assert not cond(status(db_session, et, {DEV}), "used").met
        assert cond(status(db_session, et, {other.id}), "used").met
        assert cond(status(db_session, et, None), "used").met  # the platform administrator sees all

    def test_the_endpoint_shows_the_chip_data_to_staff_who_can_read_the_clock(self, client, db_session):
        _t, _s, et = exercise_template_set(db_session)
        with acting_as(db_session, UserRole.instructor):
            body = client.get(f"/content-port/items/exercise_template/{et.id}/proven").json()
        assert body["item_type"] == "exercise_template" and body["proven"] is True and body["label"] == "Proven"
        assert {d["item_type"] for d in body["dependencies"]} == {"range_template", "scenario"}
        with acting_as(db_session, UserRole.student):
            assert client.get(f"/content-port/items/exercise_template/{et.id}/proven").status_code == 403

    def test_another_tenants_private_template_is_not_found(self, client, db_session):
        other = Tenant(id=uuid.uuid4(), name="Other", slug=f"o-{uuid.uuid4().hex[:6]}")
        db_session.add(other)
        db_session.flush()
        et = ex_template(db_session, tenant=other.id)
        with acting_as(db_session, UserRole.admin):
            assert client.get(f"/content-port/items/exercise_template/{et.id}/proven").status_code == 404

    def test_the_portable_list_includes_exercise_templates(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        with acting_as(db_session, UserRole.admin):
            rows = client.get("/content-port/items").json()
        assert ("exercise_template", str(et.id), True) in {(r["item_type"], r["item_id"], r["proven"]) for r in rows}


# ── export ───────────────────────────────────────────────────────────────────────
class TestExport:
    def test_it_carries_its_scenario_and_range_template(self, client, db_session, port):
        t, s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        assert [i["item_type"] for i in doc["items"]] == ["range_template", "scenario", "exercise_template"]
        body = item(doc, "exercise_template")
        assert sorted(body["depends_on"]) == sorted([f"range_template:{t.id}", f"scenario:{s.id}"])
        c = body["content"]
        assert c["scenario"] == f"scenario:{s.id}" and c["range_template"] == f"range_template:{t.id}"
        assert c["name"] == et.name and "kind" not in c and len(c["mesl"]) == 3
        assert c["objectives"][0]["ref"] == "O1" and c["instructor_notes"].startswith("Hold serial 2")
        assert body["proof"]["proven"] is True

    def test_roles_and_sizing_rules_are_carried_exactly(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        c = item(exported(client, db_session, ("exercise_template", et)), "exercise_template")["content"]
        assert c["roles"] == et.roles and c["scaling_rules"] == et.scaling_rules
        assert c["roles"][0]["vm_profile"] == [{"node_type": "ubuntu-2404", "count": 1}]

    def test_no_tenant_or_user_data_leaves(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        et.created_by = make_user(db_session).id
        db_session.flush()
        raw = json.dumps(exported(client, db_session, ("exercise_template", et)))
        assert str(DEV) not in raw and str(et.created_by) not in raw and "tenant" not in raw.lower()

    def test_an_unproven_scenario_refuses_the_export_and_names_it(self, client, db_session, port):
        t = prove_template(db_session, template(db_session))
        s = scenario(db_session, t)  # not proven: nothing ran it, not on the clock
        et = prove_exercise_template(db_session, ex_template(db_session, s, t))
        with acting_as(db_session, UserRole.admin):
            res = export(client, ("exercise_template", et))
        assert res.status_code == 409
        listed = res.json()["detail"]["items"]
        assert [(i["item_type"], i["item_id"], i["dependency"]) for i in listed] == [("scenario", str(s.id), True)]

    def test_an_unproven_template_is_refused(self, client, db_session, port):
        t = prove_template(db_session, template(db_session))
        et = ex_template(db_session, None, t)
        with acting_as(db_session, UserRole.admin):
            res = export(client, ("exercise_template", et))
        assert res.status_code == 409 and res.json()["detail"]["items"][0]["item_type"] == "exercise_template"

    def test_only_the_platform_admin_exports_it(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        for role in (UserRole.instructor, UserRole.range_ops, UserRole.observer, UserRole.student):
            with acting_as(db_session, role):
                assert export(client, ("exercise_template", et)).status_code == 403

    def test_another_tenants_private_template_is_not_found(self, client, db_session, port, new_site):
        et = ex_template(db_session, tenant=new_site)
        with acting_as(db_session, UserRole.admin):
            assert export(client, ("exercise_template", et)).status_code == 404

    def test_a_template_of_another_tenant_that_the_closure_reaches_is_refused(self, client, db_session, port, new_site):
        """The scenario in the template's own tenant is fine; one in another tenant is out of scope."""
        t = prove_template(db_session, template(db_session))
        theirs = scenario(db_session, template(db_session, tenant=new_site), tenant=new_site)
        et = ex_template(db_session, theirs, t)  # a corrupt record: the API never lets this be made
        review(db_session, "exercise_template", et)
        made_from(db_session, et)
        with acting_as(db_session, UserRole.admin):
            res = export(client, ("exercise_template", et))
        assert res.status_code == 409


# ── import ───────────────────────────────────────────────────────────────────────
class TestImport:
    def test_it_lands_relinked_private_and_not_yet_proven(self, client, db_session, port, new_site):
        t, s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        with acting_as(db_session, UserRole.admin):
            pre = upload(client, doc, target_tenant_id=new_site).json()
            assert [i["action"] for i in pre["items"]] == ["create", "create", "create"]
            assert db_session.query(ExerciseTemplate).filter_by(tenant_id=new_site).count() == 0  # nothing stored
            res = upload(client, doc, "import", target_tenant_id=new_site)
            assert res.status_code == 200, res.text
            assert (res.json()["created"], res.json()["skipped"]) == (3, 0)
        new_t = db_session.query(Template).filter_by(tenant_id=new_site).one()
        new_s = db_session.query(Scenario).filter_by(tenant_id=new_site).one()
        new_et = db_session.query(ExerciseTemplate).filter_by(tenant_id=new_site).one()
        assert (new_et.scenario_id, new_et.range_template_id) == (new_s.id, new_t.id)
        assert new_et.id != et.id and new_et.is_public is False and new_et.revision == 1
        assert new_et.created_by is not None  # the importing platform administrator
        assert [m["title"] for m in new_et.mesl] == ["Serial 1", "Serial 2", "Serial 3"]
        assert new_et.objectives == et.objectives and new_et.teams == et.teams and new_et.tags == et.tags
        assert new_et.roles == et.roles and new_et.scaling_rules == et.scaling_rules
        link = db_session.query(ContentPortItem).filter_by(item_type="exercise_template").one()
        assert (link.local_id, link.origin_id, link.tenant_id) == (str(new_et.id), str(et.id), new_site)
        with acting_as(db_session, UserRole.admin, new_site):
            st = client.get(f"/content-port/items/exercise_template/{new_et.id}/proven").json()
        assert st["proven"] is False and st["imported"]["proven_at_origin"] is True
        assert {c["name"] for c in st["conditions"] if not c["met"]} == {"certified", "used", "fresh"}

    def test_the_origins_rows_are_untouched(self, client, db_session, port, new_site):
        _t, _s, et = exercise_template_set(db_session)
        before = (et.name, et.revision, list(et.mesl), et.scenario_id, et.range_template_id)
        doc = exported(client, db_session, ("exercise_template", et))
        with acting_as(db_session, UserRole.admin):
            assert upload(client, doc, "import", target_tenant_id=new_site).status_code == 200
        db_session.refresh(et)
        assert (et.name, et.revision, list(et.mesl), et.scenario_id, et.range_template_id) == before

    def test_a_bundle_is_imported_once(self, client, db_session, port, new_site):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        with acting_as(db_session, UserRole.admin):
            assert upload(client, doc, "import", target_tenant_id=new_site).status_code == 200
            again = upload(client, doc, "import", target_tenant_id=new_site)
        assert again.status_code == 409 and "already imported" in again.json()["detail"]
        assert db_session.query(ExerciseTemplate).filter_by(tenant_id=new_site).count() == 1
        assert db_session.query(AuditLog).filter_by(action="content_port.bundle_rejected").count() == 1

    def test_the_same_origin_into_two_tenants_makes_two_and_never_rewrites_the_other(self, client, db_session, port,
                                                                                   new_site):
        _t, _s, et = exercise_template_set(db_session)
        second_site = Tenant(id=uuid.uuid4(), name="Second", slug=f"s-{uuid.uuid4().hex[:6]}")
        db_session.add(second_site)
        db_session.flush()
        first = exported(client, db_session, ("exercise_template", et))
        with acting_as(db_session, UserRole.admin):
            assert upload(client, first, "import", target_tenant_id=new_site).status_code == 200
        landed = db_session.query(ExerciseTemplate).filter_by(tenant_id=new_site).one()
        landed.description = "edited at the first site"
        db_session.flush()
        again = exported(client, db_session, ("exercise_template", et))
        with acting_as(db_session, UserRole.admin):
            res = upload(client, again, "import", target_tenant_id=second_site.id)
        assert res.status_code == 200 and res.json()["created"] == 3 and res.json()["updated"] == 0
        db_session.refresh(landed)
        assert landed.description == "edited at the first site" and landed.revision == 1
        assert db_session.query(ExerciseTemplate).filter_by(tenant_id=second_site.id).count() == 1

    def test_a_second_identical_export_is_unchanged(self, client, db_session, port, new_site):
        _t, _s, et = exercise_template_set(db_session)
        with acting_as(db_session, UserRole.admin):
            assert upload(client, exported(client, db_session, ("exercise_template", et)), "import",
                          target_tenant_id=new_site).status_code == 200
            pre = upload(client, exported(client, db_session, ("exercise_template", et)),
                         target_tenant_id=new_site).json()
        assert [i["action"] for i in pre["items"]] == ["unchanged"] * 3

    def test_a_newer_origin_version_updates_in_place_and_bumps_the_revision(self, client, db_session, port, new_site):
        _t, _s, et = exercise_template_set(db_session)
        with acting_as(db_session, UserRole.admin):
            upload(client, exported(client, db_session, ("exercise_template", et)), "import", target_tenant_id=new_site)
            et.description = "Origin edited it"
            et.revision = 2
            made_from(db_session, et)  # proven again: an exercise from the new revision
            db_session.flush()
            res = upload(client, exported(client, db_session, ("exercise_template", et)), "import",
                         target_tenant_id=new_site)
        assert res.status_code == 200 and (res.json()["updated"], res.json()["unchanged"]) == (1, 2)
        landed = db_session.query(ExerciseTemplate).filter_by(tenant_id=new_site).one()
        assert landed.description == "Origin edited it" and landed.revision == 2

    def test_changed_here_needs_confirmation(self, client, db_session, port, new_site):
        _t, _s, et = exercise_template_set(db_session)
        with acting_as(db_session, UserRole.admin):
            upload(client, exported(client, db_session, ("exercise_template", et)), "import", target_tenant_id=new_site)
            landed = db_session.query(ExerciseTemplate).filter_by(tenant_id=new_site).one()
            landed.instructor_notes = "Edited here"
            db_session.flush()
            et.description = "Origin edited it too"
            db_session.flush()
            doc = exported(client, db_session, ("exercise_template", et))
            pre = upload(client, doc, target_tenant_id=new_site).json()
            mine = next(i for i in pre["items"] if i["item_type"] == "exercise_template")
            assert (mine["action"], mine["conflict"]) == ("conflict", "local_changed")
            skipped = upload(client, doc, "import", target_tenant_id=new_site)
            assert skipped.json()["skipped"] == 1
            db_session.refresh(landed)
            assert landed.instructor_notes == "Edited here"
            doc2 = exported(client, db_session, ("exercise_template", et))
            done = upload(client, doc2, "import", target_tenant_id=new_site, overwrite=[mine["key"]])
        assert done.status_code == 200 and done.json()["updated"] == 1
        db_session.refresh(landed)
        assert landed.description == "Origin edited it too" and landed.instructor_notes != "Edited here"

    def test_a_same_named_template_here_needs_confirmation(self, client, db_session, port, new_site):
        _t, _s, et = exercise_template_set(db_session)
        mine = ex_template(db_session, tenant=new_site, name=et.name)
        mine.description = "Written here, no import made it"
        db_session.flush()
        doc = exported(client, db_session, ("exercise_template", et))
        with acting_as(db_session, UserRole.admin):
            pre = upload(client, doc, target_tenant_id=new_site).json()
            row = next(i for i in pre["items"] if i["item_type"] == "exercise_template")
            assert (row["action"], row["conflict"], row["local_id"]) == ("conflict", "local_name", str(mine.id))
            upload(client, doc, "import", target_tenant_id=new_site)
        db_session.refresh(mine)
        assert mine.description == "Written here, no import made it"

    def test_an_older_export_is_a_downgrade(self, client, db_session, port, new_site):
        _t, _s, et = exercise_template_set(db_session)
        old = exported(client, db_session, ("exercise_template", et))
        et.description = "Newer"
        db_session.flush()
        new = exported(client, db_session, ("exercise_template", et))
        with acting_as(db_session, UserRole.admin):
            assert upload(client, new, "import", target_tenant_id=new_site).status_code == 200
            pre = upload(client, old, target_tenant_id=new_site).json()
        row = next(i for i in pre["items"] if i["item_type"] == "exercise_template")
        assert (row["action"], row["conflict"]) == ("conflict", "older")

    def test_a_dependency_that_was_not_imported_skips_it(self, client, db_session, port, new_site):
        _t, s, et = exercise_template_set(db_session)
        clash = scenario(db_session, template(db_session, tenant=new_site), tenant=new_site, name=s.name)
        doc = exported(client, db_session, ("exercise_template", et))
        with acting_as(db_session, UserRole.admin):
            res = upload(client, doc, "import", target_tenant_id=new_site)
        out = {i["item_type"]: i for i in res.json()["items"]}
        assert out["scenario"]["action"] == "skipped" and out["exercise_template"]["action"] == "skipped"
        assert "scenario:" in " ".join(out["exercise_template"]["reasons"])
        assert db_session.query(ExerciseTemplate).filter_by(tenant_id=new_site).count() == 0
        db_session.refresh(clash)

    def test_shared_lands_in_the_tenant_and_public(self, client, db_session, port, new_site):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        with acting_as(db_session, UserRole.admin):
            assert upload(client, doc, "import", target_tenant_id=new_site, shared="true").status_code == 200
        landed = db_session.query(ExerciseTemplate).filter_by(tenant_id=new_site).one()
        assert landed.is_public is True

    def test_private_then_shared_is_a_visibility_conflict(self, client, db_session, port, new_site):
        _t, _s, et = exercise_template_set(db_session)
        with acting_as(db_session, UserRole.admin):
            upload(client, exported(client, db_session, ("exercise_template", et)), "import", target_tenant_id=new_site)
            pre = upload(client, exported(client, db_session, ("exercise_template", et)), target_tenant_id=new_site,
                         shared="true").json()
        row = next(i for i in pre["items"] if i["item_type"] == "exercise_template")
        assert (row["action"], row["conflict"]) == ("conflict", "visibility")

    def test_the_preview_diff_shows_the_template(self, client, db_session, port, new_site):
        _t, _s, et = exercise_template_set(db_session)
        with acting_as(db_session, UserRole.admin):
            pre = upload(client, exported(client, db_session, ("exercise_template", et)),
                         target_tenant_id=new_site).json()
        row = next(i for i in pre["items"] if i["item_type"] == "exercise_template")
        assert f"+name: {et.name}" in row["diff"] and "+objective O1" in row["diff"] and "+role Analyst" in row["diff"] and "+scaling Sensor" in row["diff"] and "+serial 1" in row["diff"]
        assert any("Not proven here" in n for n in row["notes"])


# ── a bundle that lies ───────────────────────────────────────────────────────────
class TestRefusals:
    def _refused(self, client, db_session, doc, text):
        with acting_as(db_session, UserRole.admin):
            res = upload(client, doc)
        assert res.status_code == 422 and text in res.json()["detail"], res.text
        assert db_session.query(ContentPortImport).count() == 0

    def test_an_unlisted_reference_is_refused(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))

        def drop(b):
            item(b, "exercise_template")["depends_on"] = []

        self._refused(client, db_session, resign(doc, drop), "does not list as dependencies")

    def test_a_reference_to_the_wrong_type_is_refused(self, client, db_session, port):
        _t, s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        self._refused(client, db_session, resign(doc, lambda b: item(b, "exercise_template")["content"].update(
            scenario=f"range_template:{s.id}")), "not a exercise_template")

    def test_a_mesl_serial_for_an_objective_it_lacks_is_refused(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        self._refused(client, db_session, resign(doc, lambda b: item(b, "exercise_template")["content"]["mesl"][0]
                                                 .update(objective_ref="NOPE")), "not a exercise_template")

    def test_an_unknown_field_or_a_tenant_id_is_refused(self, client, db_session, port, new_site):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        for extra in ({"tenant_id": str(new_site)}, {"is_public": True}, {"revision": 99}):
            self._refused(client, db_session, resign(doc, lambda b, e=extra: item(b, "exercise_template")["content"]
                                                     .update(e)), "not a exercise_template")

    @pytest.mark.parametrize("mutate", [
        lambda c: c["roles"].append({**c["roles"][0]}),  # a role twice
        lambda c: c["roles"][0].update(default_count=99),
        lambda c: c["roles"][0].update(team="Nobody"),
        lambda c: c["scaling_rules"][1].update(role="Nobody"),
        lambda c: c["scaling_rules"][1].update(per=0),
        lambda c: c["roles"][0]["vm_profile"].append({"node_type": "x", "count": 0}),
        lambda c: c["roles"][0].update(surprise=1),
    ])
    def test_malformed_roles_or_rules_are_refused(self, client, db_session, port, mutate):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        self._refused(client, db_session, resign(doc, lambda b: mutate(item(b, "exercise_template")["content"])),
                      "not a exercise_template")

    def test_a_kind_field_is_refused(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        self._refused(client, db_session, resign(doc, lambda b: item(b, "exercise_template")["content"]
                                                 .update(kind="assessment")), "not a exercise_template")

    def test_a_content_hash_mismatch_is_refused(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))

        def tamper(b):
            item(b, "exercise_template")["content"]["name"] = "Other"

        body = copy.deepcopy({k: v for k, v in doc.items() if k != "signature"})
        tamper(body)
        signed = feed_signing.sign_for(signing.CONTEXT, body, signing._signing_key())
        self._refused(client, db_session, signed, "content_sha256")

    def test_the_old_item_type_list_still_refuses_an_unknown_type(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))

        def retype(b):
            i = item(b, "exercise_template")
            i["item_type"] = "exercise"
            i["key"] = f"exercise:{i['origin_id']}"

        self._refused(client, db_session, resign(doc, retype), "not a content-port bundle")


def test_time_is_not_stored_in_the_content(client, db_session, port):
    """Review dates and staleness are this site's clock, never carried."""
    _t, _s, et = exercise_template_set(db_session)
    raw = json.dumps(exported(client, db_session, ("exercise_template", et)))
    assert "due_at" not in raw and "last_reviewed" not in raw
