"""Issue #227: the QSP qualification reads are scoped to the caller's tenant + shared.

``GET /qsp/qualifications``, ``/qsp/qualifications/{code}/objectives``,
``/qsp/developmental-progression`` and ``/qsp/curriculum-map`` queried every
``Qualification`` with no tenant predicate, so any signed-in user, a Student included,
read every tenant's qualifications and objectives. They now apply the same rule as
``tenancy.get_owned_or_global`` (onboarding): own tenant's rows plus NULL-tenant
(shared) ones. A foreign qualification's objectives are 404, never 403. A platform
admin gets no implicit cross-tenant bypass on these routes.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from app.auth import CurrentUser, get_current_user
from app.main import app as fastapi_app
from app.models import (
    PerformanceObjective,
    POTier,
    Qualification,
    RangeObjectiveMap,
    Template,
    Tenant,
    User,
    UserRole,
)
from app.rbac import is_platform_admin

DEV_TENANT = "00000000-0000-0000-0000-000000000001"  # seeded by conftest
OTHER_TENANT = "00000000-0000-0000-0000-0000000000ff"

ROLES = [UserRole.student, UserRole.instructor, UserRole.admin]


@contextmanager
def acting_as(db, role: UserRole, tenant: str = DEV_TENANT):
    """A real users row (keys are enforced) in ``tenant`` with ``role``."""
    uid = uuid.uuid4()
    db.add(User(id=uid, email=f"{uid.hex[:8]}@example.test", display_name=role.value, role=role,
                tenant_id=uuid.UUID(tenant), keycloak_id=f"kc-{uid.hex[:8]}"))
    db.commit()
    who = CurrentUser(id=str(uid), email=f"{uid.hex[:8]}@example.test", display_name=role.value,
                      role=role, tenant_id=tenant, keycloak_id=f"kc-{uid.hex[:8]}")
    fastapi_app.dependency_overrides[get_current_user] = lambda: who
    try:
        yield who
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)


def _qual(db, code: str, tenant: str | None, dp_order: int = 1) -> Qualification:
    q = Qualification(id=uuid.uuid4(), qsp_code=code, nqual=code, title=f"{code} title",
                      dp_order=dp_order, track="progression", rank_level="Pte",
                      tenant_id=uuid.UUID(tenant) if tenant else None)
    db.add(q)
    db.flush()
    db.add(PerformanceObjective(id=uuid.uuid4(), qualification_id=q.id, po_code=f"{code}_PO1",
                                title=f"secret objective of {code}", tier=POTier("core"),
                                duration_min=60, critical_events="[]"))
    db.flush()
    return q


@pytest.fixture
def spine(db_session):
    db = db_session
    db.add(Tenant(id=uuid.UUID(OTHER_TENANT), name="Second Org", slug="second-227"))
    db.flush()
    quals = dict(
        mine=_qual(db, "MINE227", DEV_TENANT, dp_order=1),
        shared=_qual(db, "SHRD227", None, dp_order=2),
        theirs=_qual(db, "THRS227", OTHER_TENANT, dp_order=3),
    )
    db.commit()
    return quals


def _codes(rows) -> set[str]:
    return set(r["qsp_code"] for r in rows)


def _map_codes(body) -> set[str]:
    return _codes(body["nodes"])


def _progression_codes(body) -> set[str]:
    return _codes(body["progression"]) | _codes(body["specialty_streams"])


VISIBLE = set(["MINE227", "SHRD227"])


@pytest.mark.parametrize("role", ROLES)
class TestTenantMemberSeesOwnAndShared:
    def test_list(self, client, db_session, spine, role):
        with acting_as(db_session, role):
            res = client.get("/qsp/qualifications")
        assert res.status_code == 200
        codes = _codes(res.json())
        assert codes >= VISIBLE
        assert "THRS227" not in codes

    def test_objectives_of_own_and_shared(self, client, db_session, spine, role):
        with acting_as(db_session, role):
            for code in sorted(VISIBLE):
                res = client.get(f"/qsp/qualifications/{code}/objectives")
                assert res.status_code == 200
                assert [po["po_code"] for po in res.json()] == [f"{code}_PO1"]

    def test_foreign_objectives_are_404_without_disclosure(self, client, db_session, spine, role):
        with acting_as(db_session, role):
            res = client.get("/qsp/qualifications/THRS227/objectives")
            # Indistinguishable from a code that exists nowhere.
            missing = client.get("/qsp/qualifications/NOPE227/objectives")
        assert res.status_code == 404
        assert "secret objective" not in res.text
        assert missing.status_code == 404

    def test_developmental_progression(self, client, db_session, spine, role):
        with acting_as(db_session, role):
            res = client.get("/qsp/developmental-progression")
        assert res.status_code == 200
        codes = _progression_codes(res.json())
        assert codes >= VISIBLE
        assert "THRS227" not in codes

    def test_curriculum_map(self, client, db_session, spine, role):
        with acting_as(db_session, role):
            res = client.get("/qsp/curriculum-map")
        assert res.status_code == 200
        body = res.json()
        assert _map_codes(body) >= VISIBLE
        assert "THRS227" not in _map_codes(body)
        assert "secret objective of THRS227" not in res.text
        assert all("THRS227" not in (e["from"], e["to"]) for e in body["edges"])


def test_the_other_tenant_sees_its_own_but_not_ours(client, db_session, spine):
    with acting_as(db_session, UserRole.student, OTHER_TENANT):
        codes = _codes(client.get("/qsp/qualifications").json())
        assert client.get("/qsp/qualifications/THRS227/objectives").status_code == 200
        assert client.get("/qsp/qualifications/MINE227/objectives").status_code == 404
    assert codes >= set(["THRS227", "SHRD227"])
    assert "MINE227" not in codes


def test_platform_admin_gets_no_cross_tenant_bypass(client, db_session, spine, monkeypatch):
    """These are tenant views and no screen needs another tenant's qualifications, so the
    platform admin is scoped like any member of their own tenant."""
    monkeypatch.setenv("PLATFORM_TENANT_ID", DEV_TENANT)
    with acting_as(db_session, UserRole.admin) as admin:
        assert is_platform_admin(admin, db_session)
        codes = _codes(client.get("/qsp/qualifications").json())
        assert client.get("/qsp/qualifications/THRS227/objectives").status_code == 404
        assert "THRS227" not in _map_codes(client.get("/qsp/curriculum-map").json())
        assert "THRS227" not in _progression_codes(client.get("/qsp/developmental-progression").json())
    assert codes >= VISIBLE
    assert "THRS227" not in codes


class TestAttachCannotPullInForeignObjectives:
    """Linking a foreign PO to an own template would read it back via /curriculum."""

    @staticmethod
    def _template(db) -> Template:
        t = Template(name=f"t-{uuid.uuid4().hex[:6]}", version="1.0", yaml="name: t\nnodes: []\n",
                     tenant_id=uuid.UUID(DEV_TENANT))
        db.add(t)
        db.commit()
        return t

    @staticmethod
    def _po_of(db, qual) -> PerformanceObjective:
        return db.query(PerformanceObjective).filter_by(qualification_id=qual.id).one()

    def test_foreign_po_is_refused_and_not_linked(self, client, db_session, spine):
        mine = self._template(db_session)
        po = self._po_of(db_session, spine["theirs"])
        res = client.post(f"/qsp/templates/{mine.id}/objectives", json=dict(po_ids=[str(po.id)]))
        assert res.status_code == 422
        assert db_session.query(RangeObjectiveMap).filter_by(template_id=mine.id).count() == 0
        curriculum = client.get(f"/qsp/templates/{mine.id}/curriculum")
        assert "secret objective of THRS227" not in curriculum.text

    def test_malformed_po_id_is_422_not_500(self, client, db_session, spine):
        mine = self._template(db_session)
        res = client.post(f"/qsp/templates/{mine.id}/objectives", json=dict(po_ids=["not-a-uuid"]))
        assert res.status_code == 422

    @pytest.mark.parametrize("which", ["mine", "shared"])
    def test_own_and_shared_pos_still_link(self, client, db_session, spine, which):
        mine = self._template(db_session)
        po = self._po_of(db_session, spine[which])
        res = client.post(f"/qsp/templates/{mine.id}/objectives", json=dict(po_ids=[str(po.id)]))
        assert res.status_code == 200
        assert res.json()["linked"] == 1
        assert client.get(f"/qsp/templates/{mine.id}/curriculum").json()["objective_count"] == 1
