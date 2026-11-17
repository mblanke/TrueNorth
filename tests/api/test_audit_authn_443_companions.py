"""Companions to the strict-xfail oracles of ``tests/api/test_audit_authn_443.py`` (audit #443).

One companion per still-failing oracle there, each pinning the same secure behaviour of the
same finding from a second angle: a two-tenant matrix, a boundary value, or the real backend
path the finding cites. Each FAILS at base for the same reason its oracle does, so each is
``xfail(strict=True)`` like its oracle: it stays xfailed until the fix lands, then starts
XPASSing and fails the suite until the marker is removed (fix + marker go together).
Run with ``--runxfail`` to see the underlying failures.

* Finding 1 (ws_auth.py:55) — the oracle opens one Moodle-launched Student's tenant channel;
  the companion runs the two-tenant matrix: each of two tenants' LTI Students must open their
  own tenant's stream, and neither may open the other tenant's.
* Finding 2 (ws_auth.py:55) — the oracle opens a full-control grant; the companion pins the
  boundary value: a ``view_only`` grant issued to an LTI Student over HTTP must open just the
  same.
* Finding 3 (ws_auth.py:59) — the oracle stubs the whole auth backend with a 503-raiser; the
  companion lets a real ``KeycloakOIDCBackend`` fail on a cold JWKS cache — its own 503
  (auth_backends/keycloak_oidc.py:72-78, re-raised cold by jwks_cache.py:75-77) — and
  requires 1013 for that path too.
* Finding 4 (rbac.py:413) has no companion here: the "a fix must not refuse the
  instructor's own tenant" guard passes at base, so it is no audit oracle; the integrator
  writes it together with the rbac fix.

The LTI fixtures follow ``tests/api/test_audit_authn_443.py::lti_world``: the identity-provider
backend is stubbed to refuse anything, because an LTI session token is signed with TrueNorth's
tool key, which no realm JWKS publishes (auth_backends/keycloak_oidc.py:100-105 wraps every
PyJWTError, including an unknown kid, into HTTPException 401).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import jwt
import pytest
from app import auth
from app.auth_backends.keycloak_oidc import KeycloakOIDCBackend
from app.console_backends import MockConsole
from app.console_backends.mock import RFB_GREETING
from app.console_relay import policy, service
from app.lti_identity import session as lti_session
from app.models import ExternalPlatform, IntegrationAuthType, Range, RangeState, Template, Tenant, User, UserRole
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette import status
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.audit443

ISSUER = "https://moodle.example.test"
TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000443c0a1")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000443c0b2")
TENANT_C = uuid.UUID("00000000-0000-0000-0000-00000443c0c3")


class RealmWithoutTheToken:
    """A realm backend facing a token it did not issue: refused (HTTP 401)."""

    async def validate_token(self, token: str) -> dict:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


def _launched_student(db_session, tenant_id: uuid.UUID, slug: str, local_part: str):
    """A Moodle-launched Student of ``tenant_id`` with a real LTI session token minted the way
    POST /lti/session issues one — the rows of tests/api/test_audit_authn_443.py::lti_world,
    for a tenant id of the caller's choosing."""
    suffix = uuid.uuid4().hex[:8]
    db_session.add(Tenant(id=tenant_id, name=slug, slug=f"{slug}-{suffix}"))
    db_session.flush()
    platform = ExternalPlatform(
        id=uuid.uuid4(), name="Moodle", slug=f"moodle-{suffix}", platform_type="moodle",
        base_url=ISSUER, auth_type=IntegrationAuthType.lti13, tenant_id=tenant_id, lti_issuer=ISSUER,
        lti_client_id="client-1", lti_deployment_id="dep-1",
        lti_auth_login_url=f"{ISSUER}/mod/lti/auth.php", lti_jwks_url=f"{ISSUER}/mod/lti/certs.php")
    db_session.add(platform)
    student = User(
        id=uuid.uuid4(), email=f"{local_part}@example.test", display_name=local_part, role=UserRole.student,
        tenant_id=tenant_id, keycloak_id=f"lti:{platform.id}:{local_part}", is_active=True)
    db_session.add(student)
    db_session.commit()
    return student, lti_session.mint_session_token(db_session, student, platform, 600)


# ── Finding 1 — ws_auth.py:55: WebSocket handshake refuses LTI session tokens ─────────


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: WebSocket handshake refuses LTI session tokens "
                                       "(control-plane/api/app/ws_auth.py:55) — companion")
def test_each_tenant_lti_student_opens_their_own_stream_and_never_another_tenants(client, db_session, monkeypatch):
    """Audit #443 finding 1 (control-plane/api/app/ws_auth.py:55, medium) — the two-tenant
    matrix companion to ``test_ws_channel_accepts_an_lti_session_token``.

    The oracle pins one Moodle-launched Student; this pins the behaviour for every tenant at
    once. ws_user() validates the bearer subprotocol by calling
    auth.get_auth_backend().validate_token() directly (ws_auth.py:55), so at base every
    tenant's LTI Student is refused; once the handshake resolves the token as
    auth.get_token_identity() does (auth.py:124-128), each tenant's Student must open their
    own tenant's stream — and still not another tenant's (main.py closes a channel the user
    may not open with 1008)."""
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "get_auth_backend", lambda: RealmWithoutTheToken())
    student_a, token_a = _launched_student(db_session, TENANT_A, "t-443c-a", "lms-a")
    student_b, token_b = _launched_student(db_session, TENANT_B, "t-443c-b", "lms-b")

    # Closed side of the matrix: tenant A's LTI Student may not open tenant B's stream.
    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(
            f"/ws/tenant.{TENANT_B}", subprotocols=["bearer", token_a]):
        pass
    assert refused.value.code == 1008, \
        f"tenant A's LTI Student opened tenant B's stream (closed {refused.value.code})"

    # Open side: each tenant's LTI Student opens their own tenant's stream, which answers,
    # rather than meeting a 1008 close.
    for tenant, token in ((TENANT_A, token_a), (TENANT_B, token_b)):
        try:
            with client.websocket_connect(f"/ws/tenant.{tenant}", subprotocols=["bearer", token]) as ws:
                ws.send_json({"action": "room_members", "room_id": "not-a-room"})
                assert ws.receive_json() == {"type": "error", "detail": "room not allowed"}
        except WebSocketDisconnect as e:
            pytest.fail(f"the handshake refused tenant {tenant}'s LTI session token: closed {e.code} {e.reason!r}; "
                        f"the same token signs the Student in over HTTP")
    assert student_a.keycloak_id.startswith("lti:") and student_b.keycloak_id.startswith("lti:")


# ── Finding 2 — ws_auth.py:55: a console grant the relay can never accept ─────────────


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: an LTI-session Student can be issued a console "
                                       "grant over HTTP that the relay WebSocket can never accept "
                                       "(control-plane/api/app/ws_auth.py:55) — companion")
def test_a_view_only_console_grant_an_lti_student_gets_opens(client, db_session, monkeypatch):
    """Audit #443 finding 2 (control-plane/api/app/ws_auth.py:55, medium) — the boundary-value
    companion to ``test_the_console_grant_an_lti_student_gets_opens``, on the ``view_only``
    kind of grant.

    The oracle pins a full-control grant; the relay's refusal is in ws_user (ws_auth.py:55),
    which never sees the grant, so a view-only grant issued to an LTI Student over HTTP —
    whose issuing dependency accepts the token — must open just the same, not close 1008."""
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "get_auth_backend", lambda: RealmWithoutTheToken())
    student, token = _launched_student(db_session, TENANT_C, "t-443c-c", "lms-c")
    tpl = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=TENANT_C)
    db_session.add(tpl)
    db_session.flush()
    vm = {"vm_id": "vm-1", "name": "abcdef12-ws1", "node_id": "ws1", "status": "running"}
    rng = Range(id=uuid.uuid4(), name="r", template_id=tpl.id, tenant_id=TENANT_C, state=RangeState.ready,
                provisioner_backend="mock", provisioner_output=json.dumps({"vms": [vm]}))
    db_session.add(rng)
    db_session.commit()
    monkeypatch.setattr(policy, "check", lambda db, grant, user: policy.Verdict(True))

    # HTTP: the dependency behind the console endpoints accepts the token ...
    ident = asyncio.run(auth.get_token_identity(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), db_session))
    assert ident.sub == student.keycloak_id

    # ... so the view-only console it names must open with the same token, not close 1008.
    access = service.issue(db_session, MockConsole(), user_id=student.id, tenant_id=TENANT_C, rng=rng, vm=vm,
                           view_only=True, observed=False, base="wss://tn.test")
    db_session.commit()
    grant = access["url"].rsplit("/", 1)[1]
    try:
        with client.websocket_connect(f"/ws/console/{grant}", subprotocols=["bearer", token]) as ws:
            assert ws.receive_bytes() == RFB_GREETING
    except WebSocketDisconnect as e:
        pytest.fail(f"the relay refused the view-only grant's own user: closed {e.code} {e.reason!r}; "
                    f"the grant was issued to this very session over HTTP")


# ── Finding 3 — ws_auth.py:59: an IdP outage is reported as "not allowed" ─────────────


@pytest.mark.audit443
@pytest.mark.xfail(strict=True, reason="audit #443: a Keycloak outage with a cold JWKS cache is "
                                       "reported to WebSocket clients as 'not allowed' (1008) "
                                       "instead of 'service unavailable' "
                                       "(control-plane/api/app/ws_auth.py:59) — companion")
def test_a_cold_jwks_on_the_real_keycloak_backend_is_not_reported_as_not_allowed(client, monkeypatch):
    """Audit #443 finding 3 (control-plane/api/app/ws_auth.py:59, low) — the real-backend
    companion to ``test_an_idp_outage_is_not_reported_as_not_allowed``.

    The oracle stubs the whole backend; this exercises the path the finding actually cites: a
    real ``KeycloakOIDCBackend`` whose JWKS fetch fails on a cold cache, which is that
    backend's own 503 (auth_backends/keycloak_oidc.py:72-78; jwks_cache.py:75-77 re-raises it
    cold). ws_user's bare ``except Exception`` (ws_auth.py:59-60) turns it into a 1008 policy
    refusal all the same: the socket must close 1013, not 1008."""
    token = jwt.encode({"sub": "kc-outage", "exp": int(time.time()) + 600}, "not-a-realm-signing-key-0123456789",
                       algorithm="HS256", headers={"kid": "unknown-to-the-cold-cache"})

    class ColdJwksKeycloak(KeycloakOIDCBackend):
        """The realm's cert endpoint not answering: the breaker opens, the fetch raises 503."""

        async def _fetch_jwks(self) -> dict:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "Authentication service temporarily unavailable")

    backend = ColdJwksKeycloak()  # its JWKS cache is cold: the 503 propagates (jwks_cache.py:76-77)
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "get_auth_backend", lambda: backend)
    with pytest.raises(WebSocketDisconnect) as e, client.websocket_connect(
            f"/ws/tenant.{TENANT_A}", subprotocols=["bearer", token]):
        pass
    assert e.value.code == 1013, f"a cold-JWKS IdP outage closed as {e.value.code}, not 1013"
