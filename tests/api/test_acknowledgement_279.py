"""Classes 2: the acknowledgement form, the rules of use everyone signs (ADR 0014 §3; #279).

The rules under test:

- Re-sign triggers: never signed; a newer version published since; the last signature of
  the current version more than 365 days old (time travel on ``rule.now``).
- Before any version is published nobody is blocked; the shipped default is a draft.
- Blocking is server-side: every route that depends on ``get_current_user`` answers 403
  ``acknowledgement_required`` until the caller signs; ``GET /auth/me`` (flag), the two
  acknowledgement endpoints and health stay open. Only those two depend on
  ``get_signed_in_user`` (a route inventory, so a new route cannot skip the check).
- Exemptions: the in-process break-glass harness; AUTH_DISABLED lanes. Not exempt: LTI
  sessions (they sign like anyone) and IdP tokens claiming a TrueNorth session.
- Ordering: roll-call admission, then disabled, then MFA, then the acknowledgement.
- A published version never changes; publishing numbers versions 1, 2, 3.
- Only the platform administrator manages versions; admins export signatures, a tenant
  admin only their tenant's, the platform administrator everyone's; CSV formulas defused.
- Publish, sign and export are audited.
- The migration adds the tables and one unpublished draft; downgrade removes them.
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from _shared import act_as, real_tenant
from app import auth, mfa_policy
from app.acknowledgement import rule, service
from app.acknowledgement.default_text import DEFAULT_TEXT
from app.acknowledgement.models import AcknowledgementSignature, AcknowledgementVersion
from app.auth import TokenPayload, get_current_user, get_signed_in_user, get_token_identity, token_from_idp_claims
from app.main import app as fastapi_app
from app.models import AuditLog, Tenant, User, UserRole
from fastapi.routing import APIRoute, iter_route_contexts

DEV_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
API = Path(__file__).resolve().parents[2] / "control-plane" / "api"


@pytest.fixture
def real_auth(monkeypatch, client):
    """The real get_current_user and /auth/me; only the token's validation is replaced."""
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    for name in ("TN_MFA_ROLES", "TN_MFA_AMR_VALUES", "TN_MFA_ACR_VALUES", "TN_MFA_DEMO_EXEMPT", "TN_TEST_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLATFORM_TENANT_ID", str(DEV_TENANT))
    mfa_policy._demo_audited.clear()
    yield client
    fastapi_app.dependency_overrides.pop(get_token_identity, None)


def _user(db, role: UserRole, *, tenant=DEV_TENANT, email: str | None = None, active: bool = True,
          keycloak_id: str | None = None) -> User:
    uid = uuid.uuid4()
    row = User(id=uid, keycloak_id=keycloak_id or f"kc-{uid}", email=email or f"{uid.hex[:10]}@example.test",
               display_name=f"{role.value} {uid.hex[:4]}", role=role, tenant_id=tenant, is_active=active)
    db.add(row)
    db.flush()
    return row


def _as(user: User, **claims) -> None:
    """Make ``user`` the token's subject. Staff get a second factor unless told otherwise."""
    claims.setdefault("amr", ["otp"])
    ident = TokenPayload(sub=user.keycloak_id, email=user.email, **claims)
    fastapi_app.dependency_overrides[get_token_identity] = lambda: ident


def _blocked(r) -> bool:
    return (
        r.status_code == 403
        and isinstance(r.json().get("detail"), dict)
        and r.json()["detail"]["code"] == "acknowledgement_required"
        and r.headers.get("X-TrueNorth-Auth-State") == "acknowledgement_required"
    )


def _published(db, text="# Rules\n\nBe good.") -> AcknowledgementVersion:
    """A version published directly (the API path is tested on its own below)."""
    admin = db.get(User, DEV_TENANT)  # the seeded dev admin (tests/conftest.py)
    draft = service.create_draft(db, admin.id, text)
    return service.publish(db, draft.id, admin.id)


def _sign(client, version: AcknowledgementVersion):
    r = client.post("/acknowledgement/sign", json={"version_id": str(version.id)})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def travel(monkeypatch):
    """Move the rule's clock: travel(days=366)."""
    base = rule.now()

    def _go(**delta):
        monkeypatch.setattr(rule, "now", lambda: base + timedelta(**delta))

    return _go


# ── Before anything is published ─────────────────────────────────────────


def test_nobody_is_blocked_before_a_version_is_published(real_auth, db_session):
    student = _user(db_session, UserRole.student)
    _as(student)
    assert real_auth.get("/auth/me").json()["acknowledgement_required"] is False
    assert real_auth.get("/users/me").status_code == 200
    body = real_auth.get("/acknowledgement/current").json()
    assert body == {"version": None, "required": False, "reason": "none_published", "signed_at": None,
                    "expires_at": None}


def test_the_default_text_is_seeded_once_as_an_unpublished_draft(real_auth, db_session):
    assert service.seed_default_draft(db_session) is True
    assert service.seed_default_draft(db_session) is False
    [row] = db_session.query(AcknowledgementVersion).all()
    assert row.text == DEFAULT_TEXT and row.version is None and row.published_at is None
    _as(_user(db_session, UserRole.student))
    assert real_auth.get("/users/me").status_code == 200  # a draft blocks nobody


def test_the_default_text_covers_use_monitoring_and_observed_consoles():
    text = DEFAULT_TEXT.lower()
    assert "monitored" in text and "observed" in text and "live screens and consoles" in text
    assert "training only" in text


# ── The three re-sign triggers ───────────────────────────────────────────


def test_never_signed_blocks_until_signed(real_auth, db_session):
    v1 = _published(db_session)
    student = _user(db_session, UserRole.student)
    _as(student)
    me = real_auth.get("/auth/me").json()
    assert me["status"] == "registered" and me["acknowledgement_required"] is True
    assert _blocked(real_auth.get("/users/me"))
    current = real_auth.get("/acknowledgement/current").json()
    assert current["required"] is True and current["reason"] == "never_signed"
    assert current["version"]["version"] == 1 and current["version"]["text"].startswith("# Rules")

    after = _sign(real_auth, v1)
    assert after["required"] is False and after["reason"] == "signed" and after["signed_at"]
    assert real_auth.get("/users/me").status_code == 200
    assert real_auth.get("/auth/me").json()["acknowledgement_required"] is False


def test_a_new_version_requires_signing_again(real_auth, db_session):
    v1 = _published(db_session)
    student = _user(db_session, UserRole.student)
    _as(student)
    _sign(real_auth, v1)
    v2 = _published(db_session, "# Rules v2")
    assert v2.version == 2
    assert _blocked(real_auth.get("/users/me"))
    assert real_auth.get("/acknowledgement/current").json()["reason"] == "new_version"
    _sign(real_auth, v2)
    assert real_auth.get("/users/me").status_code == 200


def test_a_signature_counts_for_365_days(real_auth, db_session, travel):
    v1 = _published(db_session)
    _as(_user(db_session, UserRole.student))
    signed = _sign(real_auth, v1)
    assert signed["expires_at"]
    travel(days=364)
    assert real_auth.get("/users/me").status_code == 200
    travel(days=366)
    assert _blocked(real_auth.get("/users/me"))
    assert real_auth.get("/auth/me").json()["acknowledgement_required"] is True
    assert real_auth.get("/acknowledgement/current").json()["reason"] == "expired"
    _sign(real_auth, v1)  # the yearly re-sign is of the same version
    assert real_auth.get("/users/me").status_code == 200


def test_signing_while_signed_records_nothing_new(real_auth, db_session):
    v1 = _published(db_session)
    student = _user(db_session, UserRole.student)
    _as(student)
    _sign(real_auth, v1)
    _sign(real_auth, v1)
    assert db_session.query(AcknowledgementSignature).filter_by(user_id=student.id).count() == 1


def test_only_the_current_version_can_be_signed(real_auth, db_session):
    _as(_user(db_session, UserRole.student))
    nothing = real_auth.post("/acknowledgement/sign", json={"version_id": str(uuid.uuid4())})
    assert nothing.status_code == 409
    v1 = _published(db_session)
    _published(db_session, "# v2")
    assert real_auth.post("/acknowledgement/sign", json={"version_id": str(v1.id)}).status_code == 409


def test_the_signature_records_who_where_and_which_version(real_auth, db_session):
    v1 = _published(db_session)
    student = _user(db_session, UserRole.student)
    _as(student)
    real_auth.post("/acknowledgement/sign", json={"version_id": str(v1.id)}, headers={"User-Agent": "Firefox/140"})
    sig = db_session.query(AcknowledgementSignature).filter_by(user_id=student.id).one()
    assert sig.version_id == v1.id and sig.tenant_id == DEV_TENANT and sig.user_agent == "Firefox/140"
    assert sig.ip_address  # the test client's address


# ── Blocking and the allowlist ───────────────────────────────────────────


def test_every_role_is_blocked_including_admins(real_auth, db_session):
    _published(db_session)
    for role in UserRole:
        _as(_user(db_session, role))
        assert _blocked(real_auth.get("/users/me")), role


def test_what_stays_open_while_blocked(real_auth, db_session):
    _published(db_session)
    _as(_user(db_session, UserRole.student))
    assert real_auth.get("/health").status_code == 200
    assert real_auth.get("/auth/me").status_code == 200
    assert real_auth.get("/acknowledgement/current").status_code == 200
    # Everything else, the administration of this very form included.
    assert _blocked(real_auth.get("/classes"))
    assert _blocked(real_auth.get("/acknowledgement/versions"))
    assert _blocked(real_auth.get("/acknowledgement/signatures/export"))


def test_websockets_resolve_through_the_same_check(real_auth, db_session):
    """ws_auth calls auth.get_current_user directly: the acknowledgement applies there too."""
    import asyncio

    from fastapi import HTTPException

    _published(db_session)
    student = _user(db_session, UserRole.student)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(auth.get_current_user(None, TokenPayload(sub=student.keycloak_id), db_session))
    assert caught.value.detail["code"] == "acknowledgement_required"


# Only these routes may reach a handler without the acknowledgement check.
SIGNING_ROUTES = {"getCurrentAcknowledgement", "signAcknowledgement"}


def _dependency_calls(route) -> set:
    calls, stack = set(), list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        calls.add(dep.call)
        stack.extend(dep.dependencies)
    return calls


# Every route that does NOT run get_current_user, and so never meets the acknowledgement
# check. A new one fails the test below until it is added here with a reason.
NO_CURRENT_USER_ROUTES = {
    # The two routes someone who must still sign has to reach (get_signed_in_user).
    "GET /acknowledgement/current", "POST /acknowledgement/sign",
    # Identity intake: before a users row exists; /auth/me carries the flag.
    "GET /auth/me", "POST /registration", "GET /registration/mine", "DELETE /registration/mine",
    "GET /registration/nations", "GET /registration/qualifications",
    # Unauthenticated probes.
    "GET /health", "GET /health/deep", "GET /health/live", "GET /health/ready", "GET /metrics",
    # LTI tool endpoints: platform-signed messages. The launch checks the rules itself.
    "GET /lti/jwks", "GET /lti/public-key.pem", "GET /lti/login", "POST /lti/login", "POST /lti/launch",
    "POST /lti/session", "POST /lti/deeplink/finish",
    # Moodle lab page: the session's own X-Lab-Token, minted only after the rules are signed.
    "GET /lab-access/{session_id}", "GET /lab-access/{session_id}/credentials",
    "POST /lab-access/{session_id}/console", "POST /lab-access/{session_id}/credentials/{credential_id}/reveal",
    "POST /lab-access/{session_id}/end", "POST /lab-access/{session_id}/heartbeat",
    "POST /lab-access/{session_id}/reset",
    # cmi5 AU fetch and LRS: per-registration credentials, issued by a gated launch.
    "POST /cmi5/fetch/{secret}", "GET /cmi5/lrs/{resource:path}", "POST /cmi5/lrs/{resource:path}",
    "PUT /cmi5/lrs/{resource:path}", "DELETE /cmi5/lrs/{resource:path}",
    # In-VM noise agents: per-node token, no user.
    "GET /noise/agent/plan", "POST /noise/agent/report",
    # Calendar clients: per-user feed token in the path (known exemption, rule.py).
    "GET /schedule/feed/{token}.ics",
}


def test_every_route_without_the_acknowledgement_check_is_listed():
    found = set()
    for ctx in iter_route_contexts(fastapi_app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        if get_current_user not in _dependency_calls(ctx):
            found |= {f"{m} {ctx.path}" for m in ctx.original_route.methods - {"HEAD", "OPTIONS"}}
    assert found == NO_CURRENT_USER_ROUTES, (
        f"new: {sorted(found - NO_CURRENT_USER_ROUTES)}; gone: {sorted(NO_CURRENT_USER_ROUTES - found)}")


def test_the_allowlist_test_catches_a_new_route():
    """A route that resolves its own identity (not get_signed_in_user) is still caught."""
    from fastapi import APIRouter, Depends

    probe = APIRouter()

    @probe.get("/zz-probe")
    def _probe(identity=Depends(get_token_identity)):
        return {}

    fastapi_app.include_router(probe)
    try:
        with pytest.raises(AssertionError, match="zz-probe"):
            test_every_route_without_the_acknowledgement_check_is_listed()
    finally:
        fastapi_app.router.routes.pop()


def test_only_the_signing_routes_skip_the_acknowledgement_check():
    skipping = set()
    for ctx in iter_route_contexts(fastapi_app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        calls = _dependency_calls(ctx)
        if get_signed_in_user in calls and get_current_user not in calls:
            skipping.add(ctx.original_route.operation_id or ctx.original_route.name)
    assert skipping == SIGNING_ROUTES


# ── Exemptions ───────────────────────────────────────────────────────────


def test_the_inprocess_break_glass_harness_is_never_blocked(real_auth, db_session):
    _published(db_session)
    admin = _user(db_session, UserRole.admin)
    _as(admin, amr=[], tn_session="inprocess")
    assert real_auth.get("/users/me").status_code == 200
    assert real_auth.get("/acknowledgement/versions").status_code == 200  # can still fix the form
    assert real_auth.get("/auth/me").json()["acknowledgement_required"] is False


def test_an_idp_token_cannot_claim_the_inprocess_exemption(real_auth, db_session):
    _published(db_session)
    student = _user(db_session, UserRole.student)
    ident = token_from_idp_claims({"sub": student.keycloak_id, "tn_session": "inprocess"})
    fastapi_app.dependency_overrides[get_token_identity] = lambda: ident
    assert _blocked(real_auth.get("/users/me"))


def test_an_lti_session_signs_like_anyone_and_then_goes_on(real_auth, db_session):
    """An LTI hand-off is a full Student session: the SPA shows the form, then the launch."""
    v1 = _published(db_session)
    student = _user(db_session, UserRole.student, keycloak_id=f"lti:{uuid.uuid4()}:sub-1")
    _as(student, amr=[], tn_session="lti")
    assert real_auth.get("/auth/me").json()["acknowledgement_required"] is True
    assert _blocked(real_auth.get("/users/me"))
    _sign(real_auth, v1)
    assert real_auth.get("/users/me").status_code == 200


def test_lti_tool_and_lab_token_routes_do_not_resolve_a_user():
    """LTI launches, the session exchange and the Moodle lab page authenticate without
    get_current_user, so a published version cannot strand a launch with a bare 403. The
    launch itself checks the rules of use and sends the browser to the form first (tests
    below); a lab is not provisioned, nor a lab token minted, until they are signed. /lti/links and /lti/grades are a
    signed-in user's own actions in the SPA and are gated like any other."""
    checked = 0
    for ctx in iter_route_contexts(fastapi_app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        if ctx.path.startswith(("/lti/links", "/lti/grades")):
            continue
        if ctx.path.startswith(("/lti/", "/lab-access/")):
            assert get_current_user not in _dependency_calls(ctx), ctx.path
            checked += 1
    assert checked >= 10


def test_auth_disabled_lanes_are_unchanged(client, db_session):
    assert auth.AUTH_DISABLED
    _published(db_session)
    assert client.get("/users/me").status_code == 200
    assert client.get("/auth/me").json().get("acknowledgement_required") in (False, None)


# ── Ordering: admission, disabled, MFA, then the acknowledgement ─────────


def test_mfa_is_asked_for_before_the_acknowledgement(real_auth, db_session):
    _published(db_session)
    admin = _user(db_session, UserRole.admin)
    _as(admin, amr=[], acr="1")
    for path in ("/users/me", "/acknowledgement/current"):
        r = real_auth.get(path)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "mfa_required", path
    me = real_auth.get("/auth/me").json()
    assert me["mfa_satisfied"] is False and me["acknowledgement_required"] is True  # the SPA steps up first


def test_a_disabled_account_reads_as_disabled(real_auth, db_session):
    _published(db_session)
    _as(_user(db_session, UserRole.student, active=False))
    assert real_auth.get("/users/me").json()["detail"]["code"] == "account_disabled"
    assert real_auth.get("/acknowledgement/current").json()["detail"]["code"] == "account_disabled"


def test_no_account_reads_as_unregistered(real_auth, db_session):
    _published(db_session)
    fastapi_app.dependency_overrides[get_token_identity] = lambda: TokenPayload(sub="nobody")
    assert real_auth.get("/acknowledgement/current").json()["detail"]["code"] == "registration_required"


# The roll-call path is exercised through the classes API, as tests/api/test_classes_278.py does.
def test_roll_call_admission_comes_before_the_acknowledgement(real_auth, db_session):
    from datetime import UTC, datetime

    from app.classes.models import RollCallSettings
    from app.models import Course

    v1 = _published(db_session)
    tenant = real_tenant(db_session, "ack-rc")
    db_session.add(RollCallSettings(tenant_id=tenant.id, email_domains=["corp.test", "example.test"]))
    course = Course(id=uuid.uuid4(), name="SOC", tenant_id=tenant.id, is_published=True)
    db_session.add(course)
    db_session.flush()
    lead = _user(db_session, UserRole.instructor, tenant=tenant.id)
    today = datetime.now(UTC).date()
    from app.auth import CurrentUser

    act_as(CurrentUser(id=str(lead.id), email=lead.email, display_name=lead.display_name, role=lead.role,
                       tenant_id=str(tenant.id), keycloak_id=lead.keycloak_id))
    with patch("app.notifications.get_channel") as channel:
        channel.return_value.send = _async_true
        r = real_auth.post("/classes", json={
            "name": "C201-26-09", "course_id": str(course.id), "start_date": str(today + timedelta(days=3)),
            "end_date": str(today + timedelta(days=5)), "assistant_ids": [], "observer_ids": [],
        })
        assert r.status_code == 201, r.text
        r = real_auth.post(f"/classes/{r.json()['id']}/roll-call", json={"emails": ["joe.bloggs@corp.test"]})
        assert r.status_code == 200, r.text
    fastapi_app.dependency_overrides.pop(get_current_user, None)

    joe = TokenPayload(
        sub="ad-sub-joe-ack", email="joe.bloggs@corp.test", email_verified=True, given_name="Joe",
        family_name="Bloggs", preferred_username="joe", ad_object_guid=str(uuid.uuid4()),
        ad_distinguished_name="CN=joe,OU=Users,DC=corp,DC=test",
    )
    fastapi_app.dependency_overrides[get_token_identity] = lambda: joe
    me = real_auth.get("/auth/me").json()
    assert me["status"] == "registered" and me["acknowledgement_required"] is True  # admitted, then asked
    assert _blocked(real_auth.get("/users/me"))
    _sign(real_auth, v1)
    assert real_auth.get("/users/me").status_code == 200


async def _async_true(*_a, **_k):
    return True


# ── Versions: platform administrator only, immutable once published ───────


def test_the_platform_admin_drafts_edits_and_publishes(real_auth, db_session):
    admin = db_session.get(User, DEV_TENANT)
    _as(admin)
    draft = real_auth.post("/acknowledgement/versions", json={"text": "# Draft"}).json()
    assert draft["version"] is None and draft["published_at"] is None
    edited = real_auth.put(f"/acknowledgement/versions/{draft['id']}", json={"text": "# Draft, edited"}).json()
    assert edited["text"] == "# Draft, edited"
    published = real_auth.post(f"/acknowledgement/versions/{draft['id']}/publish")
    assert published.status_code == 200 and published.json()["version"] == 1
    assert published.json()["published_by"] == str(admin.id)
    # Publishing applies to the platform administrator too.
    assert _blocked(real_auth.get("/acknowledgement/versions"))
    _sign(real_auth, db_session.get(AcknowledgementVersion, uuid.UUID(draft["id"])))
    listed = real_auth.get("/acknowledgement/versions").json()
    assert [v["version"] for v in listed] == [1]


def test_a_published_version_never_changes(real_auth, db_session):
    v1 = _published(db_session, "# Original")
    admin = db_session.get(User, DEV_TENANT)
    _as(admin)
    _sign(real_auth, v1)
    r = real_auth.put(f"/acknowledgement/versions/{v1.id}", json={"text": "# Sneaky"})
    assert r.status_code == 409
    assert real_auth.post(f"/acknowledgement/versions/{v1.id}/publish").status_code == 409
    db_session.refresh(v1)
    assert v1.text == "# Original" and v1.version == 1
    assert db_session.query(AuditLog).filter_by(action="acknowledgement_publish").count() == 1
    # The correction is a new version, which everyone signs again.
    draft = real_auth.post("/acknowledgement/versions", json={"text": "# Corrected"}).json()
    assert real_auth.post(f"/acknowledgement/versions/{draft['id']}/publish").json()["version"] == 2
    assert _blocked(real_auth.get("/users/me"))


def test_an_empty_draft_is_refused(real_auth, db_session):
    _as(db_session.get(User, DEV_TENANT))
    assert real_auth.post("/acknowledgement/versions", json={"text": ""}).status_code == 422


@pytest.mark.parametrize("role", [UserRole.student, UserRole.instructor, UserRole.observer, UserRole.range_ops])
def test_only_admins_reach_version_management(real_auth, db_session, role):
    _as(_user(db_session, role))
    assert real_auth.get("/acknowledgement/versions").status_code == 403
    assert real_auth.post("/acknowledgement/versions", json={"text": "# x"}).status_code == 403


def test_a_tenant_admin_is_not_the_platform_admin(real_auth, db_session):
    other = real_tenant(db_session, "ack-other")
    tenant_admin = _user(db_session, UserRole.admin, tenant=other.id)
    _as(tenant_admin)
    assert real_auth.get("/acknowledgement/versions").status_code == 403
    assert real_auth.post("/acknowledgement/versions", json={"text": "# x"}).status_code == 403
    draft = service.create_draft(db_session, DEV_TENANT, "# x")
    assert real_auth.post(f"/acknowledgement/versions/{draft.id}/publish").status_code == 403
    assert real_auth.put(f"/acknowledgement/versions/{draft.id}", json={"text": "# y"}).status_code == 403


# ── Export: RBAC, tenant scoping, formulas ───────────────────────────────


def _rows(r) -> list[dict]:
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    return list(csv.DictReader(io.StringIO(r.text)))


def _signed_by(db, user: User, version: AcknowledgementVersion, ua: str | None = None) -> None:
    db.add(AcknowledgementSignature(user_id=user.id, tenant_id=user.tenant_id, version_id=version.id,
                                    signed_at=rule.now(), email=user.email, display_name=user.display_name,
                                    role=user.role.value, ip_address="10.0.0.1", user_agent=ua))
    db.flush()


def test_export_is_tenant_scoped_for_a_tenant_admin_and_global_for_the_platform_admin(real_auth, db_session):
    v1 = _published(db_session)
    other = real_tenant(db_session, "ack-b")
    a_student = _user(db_session, UserRole.student, email="a.student@example.test")
    b_student = _user(db_session, UserRole.student, tenant=other.id, email="b.student@example.test")
    b_admin = _user(db_session, UserRole.admin, tenant=other.id, email="b.admin@example.test")
    platform = db_session.get(User, DEV_TENANT)
    for u in (a_student, b_student, b_admin, platform):
        _signed_by(db_session, u, v1)

    _as(b_admin)
    emails = {row["email"] for row in _rows(real_auth.get("/acknowledgement/signatures/export"))}
    assert emails == {"b.student@example.test", "b.admin@example.test"}

    _as(platform)
    rows = _rows(real_auth.get("/acknowledgement/signatures/export"))
    assert {"a.student@example.test", "b.student@example.test"} <= {row["email"] for row in rows}
    assert {row["tenant"] for row in rows} >= {other.name}
    assert rows[0]["version"] == "1" and rows[0]["ip_address"] == "10.0.0.1"


@pytest.mark.parametrize("role", [UserRole.student, UserRole.instructor, UserRole.observer, UserRole.range_ops])
def test_only_admins_export(real_auth, db_session, role):
    v1 = _published(db_session)
    user = _user(db_session, role)
    _signed_by(db_session, user, v1)
    _as(user)
    assert real_auth.get("/acknowledgement/signatures/export").status_code == 403


def test_export_defuses_spreadsheet_formulas(real_auth, db_session):
    v1 = _published(db_session)
    evil = _user(db_session, UserRole.student, email="evil@example.test")
    evil.display_name = '=HYPERLINK("http://x.test","click")'
    _signed_by(db_session, evil, v1, ua="+cmd|' /C calc'!A0")
    admin = db_session.get(User, DEV_TENANT)
    _signed_by(db_session, admin, v1)
    _as(admin)
    row = next(r for r in _rows(real_auth.get("/acknowledgement/signatures/export")) if r["email"] == "evil@example.test")
    assert row["display_name"].startswith("'=") and row["user_agent"].startswith("'+")
    for value in ("-1", "@x", "\tx", "\rx", "  =1+1", " @SUM(A1)"):
        assert service.csv_cell(value).startswith("'")
    assert service.csv_cell("plain") == "plain" and service.csv_cell(None) == ""


# ── Audit ────────────────────────────────────────────────────────────────


def test_publish_sign_and_export_are_audited(real_auth, db_session):
    admin = db_session.get(User, DEV_TENANT)
    _as(admin)
    draft = real_auth.post("/acknowledgement/versions", json={"text": "# Audited"}).json()
    real_auth.post(f"/acknowledgement/versions/{draft['id']}/publish")
    pub = db_session.query(AuditLog).filter_by(action="acknowledgement_publish").one()
    assert pub.user_id == admin.id and pub.tenant_id is None and pub.resource_id == draft["id"]
    assert json.loads(pub.detail)["version"] == 1

    student = _user(db_session, UserRole.student)
    _as(student)
    _sign(real_auth, db_session.get(AcknowledgementVersion, uuid.UUID(draft["id"])))
    signed = db_session.query(AuditLog).filter_by(action="acknowledgement_sign", user_id=student.id).one()
    assert signed.tenant_id == DEV_TENANT and signed.resource_id == draft["id"]
    assert json.loads(signed.detail)["reason"] == "never_signed"

    _as(admin)
    _sign(real_auth, db_session.get(AcknowledgementVersion, uuid.UUID(draft["id"])))
    real_auth.get("/acknowledgement/signatures/export")
    exp = db_session.query(AuditLog).filter_by(action="acknowledgement_export").one()
    assert exp.user_id == admin.id and json.loads(exp.detail) == {"rows": 2, "scope": "all"}


# ── Migration ────────────────────────────────────────────────────────────

PARENT, REVISION = "c1465c4964a6", "4c0dab4abfa8"
TABLES = {"acknowledgement_versions", "acknowledgement_signatures"}


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(API), "AUTH_DISABLED": "true"}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API, env=env, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, f"alembic {' '.join(args)} failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def test_the_revision_chains_onto_the_previous_head():
    text = (API / f"alembic/versions/{REVISION}_acknowledgement_form.py").read_text(encoding="utf-8")
    assert f'down_revision: str | None = "{PARENT}"' in text


@pytest.mark.slow
def test_migration_adds_the_tables_and_one_draft_and_downgrade_removes_them(tmp_path):
    url = f"sqlite:///{tmp_path / 'ack.db'}"
    _alembic(url, "upgrade", PARENT)
    engine = sa.create_engine(url)
    assert not TABLES & set(sa.inspect(engine).get_table_names())

    _alembic(url, "upgrade", REVISION)
    assert set(sa.inspect(engine).get_table_names()) >= TABLES
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT version, published_at, text FROM acknowledgement_versions")).all()
    assert len(rows) == 1 and rows[0][0] is None and rows[0][1] is None and rows[0][2] == DEFAULT_TEXT

    # A signature can be written through the ORM onto the migrated schema.
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        t = Tenant(id=uuid.uuid4(), name="t", slug="t")
        db.add(t)
        db.flush()
        u = User(id=uuid.uuid4(), keycloak_id="kc-x", email="x@example.test", display_name="x",
                 role=UserRole.student, tenant_id=t.id)
        db.add(u)
        db.flush()
        v = db.query(AcknowledgementVersion).one()
        v.version, v.published_at = 1, rule.now()
        db.add(AcknowledgementSignature(user_id=u.id, tenant_id=t.id, version_id=v.id, signed_at=rule.now()))
        db.commit()

    _alembic(url, "downgrade", PARENT)
    assert not TABLES & set(sa.inspect(engine).get_table_names())
    _alembic(url, "upgrade", "head")
    assert set(sa.inspect(engine).get_table_names()) >= TABLES
    engine.dispose()


def test_an_edit_racing_a_publish_cannot_change_the_published_text(real_auth, db_session):
    """update_draft is one conditional UPDATE: once the draft is published, it matches nothing."""
    admin = db_session.get(User, DEV_TENANT)
    draft = service.create_draft(db_session, admin.id, "# As read")
    service.publish(db_session, draft.id, admin.id)
    with pytest.raises(service.AcknowledgementError) as caught:
        service.update_draft(db_session, draft.id, "# Swapped after signing")
    assert caught.value.status == 409
    with pytest.raises(service.AcknowledgementError):
        service.publish(db_session, draft.id, admin.id)  # the same draft twice: once only
    db_session.refresh(draft)
    assert draft.text == "# As read" and draft.version == 1
    with pytest.raises(service.AcknowledgementError) as missing:
        service.update_draft(db_session, uuid.uuid4(), "# x")
    assert missing.value.status == 404


def test_a_user_moved_to_another_tenant_signs_again_there(real_auth, db_session):
    v1 = _published(db_session)
    student = _user(db_session, UserRole.student, email="mover@example.test")
    _as(student)
    _sign(real_auth, v1)
    other = real_tenant(db_session, "ack-new-home")
    student.tenant_id = other.id
    student.email = "mover@new.test"
    db_session.flush()
    assert _blocked(real_auth.get("/users/me"))
    _sign(real_auth, v1)
    sigs = db_session.query(AcknowledgementSignature).filter_by(user_id=student.id).all()
    assert {(s.tenant_id, s.email) for s in sigs} == {(DEV_TENANT, "mover@example.test"), (other.id, "mover@new.test")}


def test_the_old_tenant_export_shows_the_signer_as_they_were(real_auth, db_session):
    v1 = _published(db_session)
    other = real_tenant(db_session, "ack-old-home")
    b_admin = _user(db_session, UserRole.admin, tenant=other.id)
    mover = _user(db_session, UserRole.student, tenant=other.id, email="before@old.test")
    _signed_by(db_session, b_admin, v1)
    _signed_by(db_session, mover, v1)
    mover.tenant_id, mover.email = DEV_TENANT, "after@new.test"
    db_session.flush()
    _as(b_admin)
    emails = {row["email"] for row in _rows(real_auth.get("/acknowledgement/signatures/export"))}
    assert "before@old.test" in emails and "after@new.test" not in emails


# ── LTI launches open nothing before the rules of use are signed ─────────


@pytest.fixture
def launch(monkeypatch):
    from types import SimpleNamespace

    from app.routers import integrations

    labs: list[str] = []
    writes: list[str] = []
    monkeypatch.setattr(integrations.lti_links, "is_bound", lambda *a, **k: False)
    monkeypatch.setattr(integrations.lti13, "record_launch", lambda *a, **k: writes.append("launch_record"))
    monkeypatch.setattr(integrations.cmi5_lti, "launch_target",
                        lambda db, tid, user, rid: writes.append("cmi5_registration") or rid)
    monkeypatch.setattr(integrations, "_link_exercise_learner", lambda *a, **k: writes.append("exercise_learner"))
    monkeypatch.setattr(integrations, "_launch_lab", lambda db, user, rid: labs.append(rid) or "https://tn.test/labs/1#token=t")
    platform = SimpleNamespace(id=uuid.uuid4(), tenant_id=DEV_TENANT)

    def _go(db, user, kind, rid):
        monkeypatch.setattr(integrations.lti13, "parse_resource_target", lambda claims: (kind, rid))
        r = integrations._resource_link_redirect(db, platform, user, {"sub": "lms-1"}, bind_cookie=False)
        return r.headers["location"]

    _go.labs = labs
    _go.writes = writes
    return _go


def test_a_lab_launch_provisions_nothing_until_the_rules_are_signed(real_auth, db_session, launch):
    v1 = _published(db_session)
    student = _user(db_session, UserRole.student)
    where = launch(db_session, student, "lab", f"{uuid.uuid4()}:lab-1")
    assert where.endswith("/rules-of-use") and launch.labs == []  # no lab, no X-Lab-Token
    _signed_by(db_session, student, v1)
    where = launch(db_session, student, "lab", "c:lab-1")
    assert "#token=" in where and launch.labs == ["c:lab-1"]


def test_any_other_launch_goes_to_the_rules_first_then_to_what_was_launched(real_auth, db_session, launch):
    _published(db_session)
    student = _user(db_session, UserRole.student)
    where = launch(db_session, student, "quiz", "q1")
    assert "/rules-of-use?returnUrl=%2Fquiz-player%3Fquiz%3Dq1%26lti%3D1" in where


@pytest.mark.parametrize("kind,rid", [("cmi5", "rel:0"), ("exercise", str(uuid.uuid4())), ("lab", "c:lab-1")])
def test_a_launch_writes_nothing_until_the_rules_are_signed(real_auth, db_session, launch, kind, rid):
    _published(db_session)
    student = _user(db_session, UserRole.student)
    where = launch(db_session, student, kind, rid)
    assert where.endswith("/rules-of-use") and launch.writes == [] and launch.labs == []


def test_a_signed_cmi5_launch_registers_and_records(real_auth, db_session, launch, monkeypatch):
    from app.routers import integrations

    monkeypatch.setattr(integrations.cmi5_lti, "spa_path", lambda rid: "/au/releases/r?launch=0&lti=1")
    v1 = _published(db_session)
    student = _user(db_session, UserRole.student)
    _signed_by(db_session, student, v1)
    assert launch(db_session, student, "cmi5", "rel:0").endswith("/au/releases/r?launch=0&lti=1")
    assert launch.writes == ["cmi5_registration", "launch_record"]


@pytest.fixture
def deep_link(monkeypatch, db_session):
    from types import SimpleNamespace

    from app.routers import integrations

    platform = SimpleNamespace(id=uuid.uuid4(), tenant_id=DEV_TENANT, name="moodle")
    picked: list[bool] = []

    async def _validate(db, id_token, state):
        return platform, {integrations.lti13.CLAIM_MESSAGE_TYPE: "LtiDeepLinkingRequest", "sub": "s"}

    monkeypatch.setattr(integrations, "_state_cookie_required", lambda: False)
    monkeypatch.setattr(integrations.lti13, "validate_launch", _validate)
    monkeypatch.setattr(integrations, "_deep_link_picker",
                        lambda db, p, c: picked.append(True) or integrations.HTMLResponse("picker"))

    def _go(user):
        import asyncio

        monkeypatch.setattr(integrations, "_jit_user", lambda db, p, c: user)
        return asyncio.run(integrations.lti_launch(request=None, id_token="t", state="s", db=db_session))

    _go.picked = picked
    return _go


def test_deep_linking_waits_for_the_rules_of_use(real_auth, db_session, deep_link):
    v1 = _published(db_session)
    instructor = _user(db_session, UserRole.instructor)
    r = deep_link(instructor)
    assert r.status_code == 403 and deep_link.picked == []
    assert b"Sign the rules of use in TrueNorth first" in r.body and b"/rules-of-use" in r.body
    _signed_by(db_session, instructor, v1)
    assert deep_link(instructor).body == b"picker" and deep_link.picked == [True]


# ── A version that renders blank must never be publishable ───────────────


@pytest.mark.parametrize("text", [" ", "\n\t", "<!-- nothing -->", "---", "***\n\n---", "![logo](/a.png)",
                                  "<img src=x>", "#", "> ", "<div></div>", "| | |\n|-|-|"])
def test_a_draft_with_no_visible_text_is_refused(real_auth, db_session, text):
    _as(db_session.get(User, DEV_TENANT))
    assert real_auth.post("/acknowledgement/versions", json={"text": text}).status_code == 422
    draft = service.create_draft(db_session, DEV_TENANT, "# Fine")
    assert real_auth.put(f"/acknowledgement/versions/{draft.id}", json={"text": text}).status_code == 422


def test_a_blank_draft_already_stored_cannot_be_published(real_auth, db_session):
    blank = AcknowledgementVersion(text="<!-- x -->")
    db_session.add(blank)
    db_session.flush()
    _as(db_session.get(User, DEV_TENANT))
    assert real_auth.post(f"/acknowledgement/versions/{blank.id}/publish").status_code == 422
    assert rule.current_version(db_session) is None


@pytest.mark.parametrize("text", ["a", "# Rules", "[Read this](/wiki/rules)", "Agree."])
def test_any_visible_text_is_enough(text):
    assert service.has_visible_text(text)


# ── Break-glass script: sign or republish from the host ──────────────────


def _breakglass():
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("ack_bg", root / "scripts/acknowledgement_breakglass_inprocess.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_breakglass_signs_for_a_stuck_user_and_audits_it(real_auth, db_session):
    v1 = _published(db_session)
    admin = _user(db_session, UserRole.admin, email="stuck.admin@example.test")
    bg = _breakglass()
    out = bg.sign(db_session, email="stuck.admin@example.test", reason="blank version", operator="root")
    assert "signed version 1" in out
    assert rule.status(db_session, admin.id, admin.tenant_id).required is False
    note = db_session.query(AuditLog).filter_by(action="acknowledgement_breakglass").one()
    assert json.loads(note.detail)["operator"] == "root" and json.loads(note.detail)["what"] == "sign"
    sig = db_session.query(AcknowledgementSignature).filter_by(user_id=admin.id).one()
    assert sig.version_id == v1.id and sig.user_agent == "inprocess breakglass (root)"


def test_breakglass_publishes_the_shipped_text_over_a_bad_version(real_auth, db_session):
    _published(db_session)
    platform = db_session.get(User, DEV_TENANT)
    bg = _breakglass()
    out = bg.publish_default(db_session, email=platform.email, reason="v1 renders blank", operator="root")
    assert "published version 2" in out
    current = rule.current_version(db_session)
    assert current.version == 2 and current.text == DEFAULT_TEXT
    assert db_session.query(AuditLog).filter_by(action="acknowledgement_breakglass").count() == 1
    with pytest.raises(SystemExit):
        bg.publish_default(db_session, email="nobody@example.test", reason="x", operator="root")


# ── Migration text and reason labels ─────────────────────────────────────


def test_the_migration_seeds_its_own_copy_of_the_text():
    text = (API / f"alembic/versions/{REVISION}_acknowledgement_form.py").read_text(encoding="utf-8")
    assert "from app.acknowledgement" not in text  # later edits to the app's text never change an old migration
    import importlib.util

    spec = importlib.util.spec_from_file_location("_ack_mig", API / f"alembic/versions/{REVISION}_acknowledgement_form.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.SEED_TEXT == DEFAULT_TEXT  # identical today


def test_a_user_new_to_a_tenant_reads_as_never_signed_there(real_auth, db_session):
    v1 = _published(db_session)
    student = _user(db_session, UserRole.student)
    _signed_by(db_session, student, v1)
    other = real_tenant(db_session, "ack-label")
    student.tenant_id = other.id
    db_session.flush()
    assert rule.status(db_session, student.id, other.id).reason == "never_signed"
