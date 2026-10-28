"""Second adversarial review of #342 (ADR 0017): the VM cap counts scaling rules too, no
tenant-private name leaks to a platform administrator's view or an export refusal, the redaction
is case-insensitive and whole-name, a deleted exercise stops proving, and an import locks the
template row it updates."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from _content_port_kit import (
    DEV,
    acting_as,
    course,
    ex_template,
    exercise_template_set,
    made_from,
    scenario,
    template,
)
from app.content_port import proven, signing
from app.exercise_templates.models import ExerciseTemplate, ExerciseTemplateUse
from app.models import Exercise, Tenant, UserRole
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


# ── the VM cap counts the scaling rules ──────────────────────────────────────────
def rule(name="R", kind="fixed", count=1, **kw):
    return {"name": name, "node_type": "x", "kind": kind, "count": count, **kw}


ROLE = {"name": "A", "max_participants": 10, "default_count": 10, "vm_profile": [{"node_type": "x", "count": 1}]}


class TestCapCountsRules:
    def _post(self, client, db_session, roles, rules):
        with acting_as(db_session, UserRole.admin):
            return client.post("/exercise-templates", json=payload(roles=roles, scaling_rules=rules))

    def test_one_participant_and_a_hundred_big_fixed_rules_is_refused(self, client, db_session):
        role = {"name": "A", "max_participants": 1, "default_count": 1, "vm_profile": [{"node_type": "x"}]}
        rules = [rule(f"R{i}", count=1000) for i in range(100)]
        res = self._post(client, db_session, [role], rules)
        assert res.status_code == 422 and "VMs" in res.text and "1000" in res.text

    def test_fixed_rules_add_their_count(self, client, db_session):
        # 10 role VMs + 990 fixed = 1000: allowed; one more is not
        assert self._post(client, db_session, [ROLE], [rule(count=990)]).status_code == 201
        assert self._post(client, db_session, [ROLE], [rule(count=991)]).status_code == 422

    def test_per_participants_rules_use_the_roles_maximum_when_a_role_is_named(self, client, db_session):
        # role A max 10, one VM each = 10; "2 per 1 participant of A" = 2 * 10 = 20 more
        ok = rule(kind="per_participants", count=2, per=1, role="A")
        assert self._post(client, db_session, [ROLE], [ok]).status_code == 201
        big = rule(kind="per_participants", count=100, per=1, role="A")  # 100 * 10 = 1000 > 990 left
        assert self._post(client, db_session, [ROLE], [big]).status_code == 422

    def test_per_participants_rules_round_up_in_steps(self, client, db_session):
        # 10 participants in steps of 3 -> ceil(10/3) = 4 steps; 4 * 249 = 996 + 10 role VMs > 1000
        r = rule(kind="per_participants", count=249, per=3, role="A")
        assert self._post(client, db_session, [ROLE], [r]).status_code == 422
        r = rule(kind="per_participants", count=247, per=3, role="A")  # 988 + 10 = 998
        assert self._post(client, db_session, [ROLE], [r]).status_code == 201

    def test_without_a_role_the_templates_total_participants_count(self, client, db_session):
        roles = [ROLE, {"name": "B", "max_participants": 0, "default_count": 15}]  # 10 + 15 = 25 (B at its default)
        # ceil(25/5) = 5 steps * 200 = 1000, plus role VMs 10 -> over
        r = rule(kind="per_participants", count=200, per=5)
        assert self._post(client, db_session, roles, [r]).status_code == 422
        r = rule(kind="per_participants", count=150, per=5)  # 750 + 10
        assert self._post(client, db_session, roles, [r]).status_code == 201

    def test_per_segment_rules_use_the_documented_segment_bound(self, client, db_session, monkeypatch):
        # MAX_SEGMENTS defaults to 10
        assert self._post(client, db_session, [ROLE], [rule(kind="per_segment", count=99)]).status_code == 201
        assert self._post(client, db_session, [ROLE], [rule(kind="per_segment", count=100)]).status_code == 422
        monkeypatch.setenv("TN_EXERCISE_TEMPLATE_MAX_SEGMENTS", "2")
        assert self._post(client, db_session, [ROLE], [rule(kind="per_segment", count=400)]).status_code == 201

    def test_the_rules_are_counted_on_update_too(self, client, db_session):
        with acting_as(db_session, UserRole.admin):
            tid = client.post("/exercise-templates", json=payload(roles=[ROLE], scaling_rules=[])).json()["id"]
            res = client.put(f"/exercise-templates/{tid}", json={"scaling_rules": [rule(count=1000)]})
        assert res.status_code == 422

    def test_a_bundle_is_held_to_the_same_total(self, client, db_session, port):
        _t, _s, et = exercise_template_set(db_session)
        doc = exported(client, db_session, ("exercise_template", et))
        bad = resign(doc, lambda b: item(b, "exercise_template")["content"]["scaling_rules"].append(
            {"name": "Huge", "node_type": "x", "kind": "fixed", "count": 1000, "per": 0, "role": "", "segment": ""}))
        with acting_as(db_session, UserRole.admin):
            res = upload(client, bad)
        assert res.status_code == 422


# ── names of another tenant's private content ────────────────────────────────────
class TestNoLeakFromThePlatformAdmin:
    def test_the_export_refusal_does_not_name_another_tenants_private_range_template(self, client, db_session, port,
                                                                                    other):
        t = template(db_session, tenant=other, name="SECRET-PRIVATE-RT")
        et = ex_template(db_session, None, t, tenant=other, public=True)
        with acting_as(db_session, UserRole.admin):  # the platform administrator, of another tenant
            res = client.post("/content-port/export",
                              json={"items": [{"item_type": "exercise_template", "item_id": str(et.id)}]})
        assert res.status_code == 409 and "SECRET-PRIVATE" not in res.text

    def test_the_proven_view_and_the_list_do_not_name_it_either(self, client, db_session, port, other):
        t = template(db_session, tenant=other, name="SECRET-PRIVATE-RT")
        et = ex_template(db_session, None, t, tenant=other, public=True)
        with acting_as(db_session, UserRole.admin):
            for url in (f"/content-port/items/exercise_template/{et.id}/proven", "/content-port/items",
                        f"/exercise-templates/{et.id}", "/exercise-templates"):
                res = client.get(url)
                assert res.status_code == 200 and "SECRET-PRIVATE" not in res.text, url

    def test_a_tenant_admin_sees_no_private_names_through_a_shared_template_or_scenario(self, client, db_session,
                                                                                       port, other):
        t = template(db_session, name="SECRET-PRIVATE-RT")
        sc = scenario(db_session, t, name="SECRET-SC")
        et = ex_template(db_session, sc, t, public=True)
        sc.is_public = True
        course(db_session, sc, name="SECRET-COURSE")
        db_session.flush()
        with acting_as(db_session, UserRole.admin, other):
            for url in (f"/content-port/items/exercise_template/{et.id}/proven",
                        f"/content-port/items/scenario/{sc.id}/proven", "/exercise-templates",
                        f"/exercise-templates/{et.id}/exercises"):
                res = client.get(url)
                assert res.status_code == 200 and "SECRET-PRIVATE" not in res.text, (url, res.text[:400])

    def test_the_owner_platform_admin_still_sees_its_own_names(self, client, db_session, port):
        t, s, et = exercise_template_set(db_session)
        with acting_as(db_session, UserRole.admin):
            deps = client.get(f"/content-port/items/exercise_template/{et.id}/proven").json()["dependencies"]
        assert {d["title"] for d in deps} == {t.name, s.name}


class TestRedaction:
    def _view(self, db_session, other, names):
        t, s, et = exercise_template_set(db_session)
        t.name, s.name = names
        et.is_public = True
        db_session.flush()
        st = proven.status(db_session, "exercise_template", et, {other}, other)
        return " ".join(st.reasons) + " ".join(str(d.title) for d in st.dependencies)

    def test_a_different_case_is_hidden_too(self, db_session, other):
        text = self._view(db_session, other, ("Secret Lab", "Hidden Scenario"))
        assert "secret lab" not in text.lower() and "hidden scenario" not in text.lower()

    def test_a_name_that_is_a_substring_of_another_hidden_name_is_hidden_whole(self, db_session, other):
        text = self._view(db_session, other, ("Secret Lab Two", "Secret Lab"))
        assert "secret" not in text.lower()

    def test_a_word_that_merely_contains_the_name_is_left_alone(self, db_session, other):
        t = template(db_session, name="reach")  # never certified: its reason says "has reached Certified"
        et = ex_template(db_session, None, t, public=True)
        text = " ".join(proven.status(db_session, "exercise_template", et, {other}, other).reasons)
        assert "has reached Certified" in text and "range template reach:" not in text.lower()


# ── proof stops with a deleted exercise ──────────────────────────────────────────
def test_a_deleted_exercise_no_longer_proves_it(db_session):
    _t, _s, et = exercise_template_set(db_session)
    assert proven.status(db_session, "exercise_template", et, {DEV}).proven
    for use in db_session.query(ExerciseTemplateUse).filter_by(template_id=et.id):
        db_session.get(Exercise, use.exercise_id).deleted_at = datetime.now(UTC)
    db_session.flush()
    assert not proven.status(db_session, "exercise_template", et, {DEV}).proven


# ── an import locks the row it updates ───────────────────────────────────────────
def test_an_import_update_locks_the_template_row(client, db_session, port, other, monkeypatch):
    from sqlalchemy.orm import Query

    _t, _s, et = exercise_template_set(db_session)
    with acting_as(db_session, UserRole.admin):
        upload(client, exported(client, db_session, ("exercise_template", et)), "import", target_tenant_id=other)
        landed = db_session.query(ExerciseTemplate).filter_by(tenant_id=other).one()
        et.description = "Origin edited it"
        et.revision = 2
        made_from(db_session, et)
        db_session.flush()
        locked: list[str] = []
        real = Query.with_for_update

        def spy(self, *a, **k):
            locked.append(self.column_descriptions[0]["entity"].__name__)
            return real(self, *a, **k)

        monkeypatch.setattr(Query, "with_for_update", spy)
        res = upload(client, exported(client, db_session, ("exercise_template", et)), "import",
                     target_tenant_id=other)
    assert res.status_code == 200 and res.json()["updated"] >= 1
    assert "ExerciseTemplate" in locked
    db_session.refresh(landed)
    assert landed.revision == 2
