"""Second-tenant 404s for the lookups the widened scoping guard found (2026-09-27).

The static guard (test_tenant_scoping_guard.py) only matched `.first()/.all()` with
`Model.id ==`, so `filter_by(id=...).one_or_none()` lookups in qsp.py,
exercises_collective.py and golden_images.py leaked across tenants while it stayed
green. These tests seed a foreign tenant's rows and assert the dev-mode caller
(DEV_TENANT) gets 404, never the row, and cannot write through them.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    Exercise,
    ExerciseState,
    GoldenImage,
    PerformanceObjective,
    POTier,
    Qualification,
    Range,
    RangeObjectiveMap,
    Template,
    UserRole,
)

DEV_TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"


@contextmanager
def acting_as(role: UserRole, tenant: str = DEV_TENANT):
    who = CurrentUser(id=str(uuid.uuid4()), email=f"{role.value}@example.test", display_name=role.value,
                      role=role, tenant_id=tenant, keycloak_id=f"kc-{uuid.uuid4().hex[:8]}")
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _template(db, tenant: str | None, *, public: bool = False) -> Template:
    t = Template(name=f"t-{uuid.uuid4().hex[:6]}", version="1.0", yaml="name: t\nnodes: []\n",
                 tenant_id=uuid.UUID(tenant) if tenant else None, is_public=public)
    db.add(t)
    db.flush()
    return t


def _range(db, tenant: str, template: Template) -> Range:
    r = Range(name=f"r-{uuid.uuid4().hex[:6]}", template_id=template.id, tenant_id=uuid.UUID(tenant))
    db.add(r)
    db.flush()
    return r


def _po(db) -> PerformanceObjective:
    qual = Qualification(id=uuid.uuid4(), qsp_code="ALJQ", nqual=f"NQ-{uuid.uuid4().hex[:4]}", dp_order=1,
                         track="t", rank_level=1, title="q")
    db.add(qual)
    db.flush()
    po = PerformanceObjective(id=uuid.uuid4(), qualification_id=qual.id, po_code="PO_1", title="po",
                              tier=POTier("core"), duration_min=60, critical_events="[]")
    db.add(po)
    db.flush()
    return po


# ── QSP: template curriculum and PO links ─────────────────────────────────


class TestTemplateCurriculum:
    def test_foreign_template_curriculum_is_404(self, client, db_session):
        theirs = _template(db_session, OTHER_TENANT)
        db_session.commit()
        assert client.get(f"/qsp/templates/{theirs.id}/curriculum").status_code == 404

    @pytest.mark.parametrize("tenant,public", [(DEV_TENANT, False), (None, False), (OTHER_TENANT, True)])
    def test_own_catalogue_and_public_templates_stay_readable(self, client, db_session, tenant, public):
        t = _template(db_session, tenant, public=public)
        db_session.commit()
        assert client.get(f"/qsp/templates/{t.id}/curriculum").status_code == 200

    def test_malformed_id_is_404_not_500(self, client):
        assert client.get("/qsp/templates/not-a-uuid/curriculum").status_code == 404


class TestTemplateObjectiveLinks:
    def test_cannot_link_pos_onto_a_foreign_template(self, client, db_session):
        theirs = _template(db_session, OTHER_TENANT)
        po = _po(db_session)
        db_session.commit()
        res = client.post(f"/qsp/templates/{theirs.id}/objectives", json={"po_ids": [str(po.id)]})
        assert res.status_code == 404
        assert db_session.query(RangeObjectiveMap).filter_by(template_id=theirs.id).count() == 0

    @pytest.mark.parametrize("tenant,public", [(OTHER_TENANT, True), (None, False)])
    def test_shared_templates_are_read_only(self, client, db_session, tenant, public):
        shared = _template(db_session, tenant, public=public)
        po = _po(db_session)
        db_session.commit()
        res = client.post(f"/qsp/templates/{shared.id}/objectives", json={"po_ids": [str(po.id)]})
        assert res.status_code == 404

    def test_cannot_unlink_pos_from_a_foreign_template(self, client, db_session):
        theirs = _template(db_session, OTHER_TENANT)
        po = _po(db_session)
        db_session.add(RangeObjectiveMap(template_id=theirs.id, po_id=po.id, source="manual",
                                         tenant_id=uuid.UUID(OTHER_TENANT)))
        db_session.commit()
        res = client.delete(f"/qsp/templates/{theirs.id}/objectives/{po.id}")
        assert res.status_code == 404
        assert db_session.query(RangeObjectiveMap).filter_by(template_id=theirs.id).count() == 1

    def test_own_template_link_and_unlink_still_work(self, client, db_session):
        mine = _template(db_session, DEV_TENANT)
        po = _po(db_session)
        db_session.commit()
        assert client.post(f"/qsp/templates/{mine.id}/objectives",
                           json={"po_ids": [str(po.id)]}).json()["linked"] == 1
        assert client.get(f"/qsp/templates/{mine.id}/curriculum").json()["objective_count"] == 1
        assert client.delete(f"/qsp/templates/{mine.id}/objectives/{po.id}").json()["detached"] is True

    @pytest.mark.parametrize("role", [UserRole.student, UserRole.observer])
    def test_read_only_roles_cannot_change_links(self, client, db_session, role):
        mine = _template(db_session, DEV_TENANT)
        po = _po(db_session)
        db_session.commit()
        with acting_as(role):
            assert client.post(f"/qsp/templates/{mine.id}/objectives",
                               json={"po_ids": [str(po.id)]}).status_code == 403
            assert client.delete(f"/qsp/templates/{mine.id}/objectives/{po.id}").status_code == 403
            assert client.get(f"/qsp/templates/{mine.id}/curriculum").status_code == 200


class TestRangeCurriculum:
    def test_foreign_range_curriculum_is_404(self, client, db_session):
        theirs = _range(db_session, OTHER_TENANT, _template(db_session, OTHER_TENANT))
        db_session.commit()
        assert client.get(f"/qsp/ranges/{theirs.id}/curriculum").status_code == 404

    def test_own_range_on_a_shared_template_is_readable(self, client, db_session):
        mine = _range(db_session, DEV_TENANT, _template(db_session, None))
        db_session.commit()
        body = client.get(f"/qsp/ranges/{mine.id}/curriculum").json()
        assert body["range_id"] == str(mine.id)

    def test_malformed_range_id_is_404(self, client):
        assert client.get("/qsp/ranges/nope/curriculum").status_code == 404


# ── Collective exercises ──────────────────────────────────────────────────


def _collective(db, tenant: str) -> Exercise:
    rng = _range(db, tenant, _template(db, tenant))
    ex = Exercise(name=f"coll-{uuid.uuid4().hex[:4]}", kind="collective", range_id=rng.id,
                  state=ExerciseState.pending, tenant_id=uuid.UUID(tenant))
    db.add(ex)
    db.flush()
    return ex


class TestCollectiveExercises:
    def test_foreign_exercise_is_404_on_every_route(self, client, db_session):
        theirs = _collective(db_session, OTHER_TENANT)
        db_session.commit()
        eid = theirs.id
        assert client.get(f"/collective-exercises/{eid}").status_code == 404
        files = {"file": ("o.csv", b"ref,text\nO1,x\n", "text/csv")}
        assert client.post(f"/collective-exercises/{eid}/objectives/import", files=files).status_code == 404
        files = {"file": ("m.csv", b"serial,time\n1,00:00\n", "text/csv")}
        assert client.post(f"/collective-exercises/{eid}/mesl/import", files=files).status_code == 404
        assert client.patch(f"/collective-exercises/{eid}/mesl/{uuid.uuid4()}", json={}).status_code == 404
        assert client.post(f"/collective-exercises/{eid}/mesl/generate", json={}).status_code == 404

    def test_list_excludes_foreign_exercises(self, client, db_session):
        mine, theirs = _collective(db_session, DEV_TENANT), _collective(db_session, OTHER_TENANT)
        db_session.commit()
        ids = {row["id"] for row in client.get("/collective-exercises").json()}
        assert str(mine.id) in ids and str(theirs.id) not in ids

    def test_cannot_create_an_exercise_on_a_foreign_range(self, client, db_session):
        theirs = _range(db_session, OTHER_TENANT, _template(db_session, OTHER_TENANT))
        db_session.commit()
        res = client.post("/collective-exercises", json={"name": "x", "range_id": str(theirs.id)})
        assert res.status_code == 422
        assert db_session.query(Exercise).filter_by(range_id=theirs.id).count() == 0

    def test_own_exercise_still_readable(self, client, db_session):
        mine = _collective(db_session, DEV_TENANT)
        db_session.commit()
        assert client.get(f"/collective-exercises/{mine.id}").status_code == 200


# ── Golden-image registry: platform-wide, so gated on permission not tenant ──


class TestGoldenImageWrites:
    def _image(self, db) -> GoldenImage:
        img = GoldenImage(catalogue_id=f"img-{uuid.uuid4().hex[:6]}", hypervisor="vsphere",
                          template_name="x", tenant_id=uuid.UUID(OTHER_TENANT))
        db.add(img)
        db.commit()
        return img

    @pytest.mark.parametrize("role", [UserRole.student, UserRole.observer, UserRole.instructor])
    def test_non_operators_cannot_edit_the_registry(self, client, db_session, role):
        img = self._image(db_session)
        with acting_as(role):
            assert client.patch(f"/golden-images/{img.id}", json={"template_name": "evil"}).status_code == 403
        db_session.refresh(img)
        assert img.template_name == "x"

    @pytest.mark.parametrize("role", [UserRole.range_ops, UserRole.admin])
    def test_operators_can_edit_it_across_tenants(self, client, db_session, role):
        img = self._image(db_session)
        with acting_as(role):
            res = client.patch(f"/golden-images/{img.id}", json={"template_name": "ubuntu-2404-cloud"})
        assert res.status_code == 200, res.text
