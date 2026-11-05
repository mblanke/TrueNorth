"""One failing test per still-present confirmed finding of audit #443 (authentication,
RBAC and WebSocket sign-in), so the later fixes have an oracle to land against. Every test
below FAILS at base 280a838b and asserts the correct behaviour; each docstring names its
finding (file:line, title as filed).

Per the integrator decision of 2026-11-05 the failing tests are marked
``xfail(strict=True)`` so the oracle merges green: each stays xfailed until its fix lands,
then starts XPASSing and fails the suite until the marker is removed (fix + marker go
together). Run with ``--runxfail`` to see the underlying failures.

Findings and their state at this base:

* FIXED — "A console closes when its sign-in token expires" (console_relay/router.py):
  fixed in #442 (commit 54dca7af, a console now outlives its 300 s token and closes only at
  the 8 h MAX_OPEN_SECONDS ceiling, console_relay/router.py:109). Not re-tested here:
  tests/api/test_console_relay.py
  already pins it (``test_the_console_outlives_the_sign_in_token_it_was_opened_with`` and
  ``test_a_console_open_past_the_ceiling_closes_1001``).
* FAILING — findings 1 and 2 (ws_auth.py:55): the WebSocket handshake validates the bearer
  subprotocol against the identity-provider backend only; the LTI session branch that every
  HTTP route goes through (auth.get_token_identity, auth.py:124-128) is never reached, so a
  Moodle-launched Student cannot open any WebSocket.
* FAILING — finding 3 (ws_auth.py:59): the bare ``except Exception`` reports an identity
  provider outage as "not allowed" (1008) instead of "service unavailable".
* FAILING — finding 4 (rbac.py:413): require_tenant_access declares ``**kwargs`` and so can
  never see the path parameter it checks. On the pinned FastAPI (0.142.2) that fails closed
  (422: ``kwargs`` is demanded as a query parameter); on a release that silently ignores a
  ``**kwargs`` parameter it would fail open — either way the documented usage
  (rbac.py:390-398) yields no tenant isolation anywhere it is adopted.

Note for whoever lands the fixes: lti_session.verify() returns no ``exp`` claim
(lti_identity/session.py:177-183), so a ws_user fix that routes through it alone leaves
``WsIdentity.expires_at`` None — carry the token's ``exp`` through (finding 1's scenario
names the 2 h session lifetime; websocket_manager closes expired sockets).

Every stubbed identity-provider backend here raises 401 like the real realm backends do for
a token they did not issue (auth_backends/keycloak_oidc.py:100-105 wraps every PyJWTError,
including an unknown kid, into HTTPException 401) — the same idiom as the ``auth_on`` and
``world`` fixtures of test_ws_auth.py and test_console_relay.py.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from app import auth, rbac
from app.console_backends import MockConsole
from app.console_relay import policy, service
from app.lti_identity import session as lti_session
from app.models import ExternalPlatform, IntegrationAuthType, Range, RangeState, Template, Tenant, User, UserRole
from starlette import status
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.audit443

ISSUER = "https://moodle.example.test"
TENANT = uuid.UUID("00000000-0000-0000-0000-000004430001")


class RealmWithoutTheToken:
    """A realm backend facing a token it did not issue: refused (HTTP 401)."""

    async def validate_token(self, token: str) -> dict:
        from fastapi import HTTPException

        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


@pytest.fixture
def lti_world(db_session, monkeypatch):
    """A Moodle-launched Student — keycloak_id ``lti:<platform>:<sub>``, no Keycloak
    sign-in (lti_identity/session.py:needs_handoff) — with a real LTI session token minted
    the way POST /lti/session issues one. The IdP backend is stubbed to refuse anything:
    the session token is signed with TrueNorth's tool key, which no realm JWKS publishes."""
    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "get_auth_backend", lambda: RealmWithoutTheToken())
    db_session.add(Tenant(id=TENANT, name="t-443", slug="t-443"))
    db_session.flush()
    platform = ExternalPlatform(
        id=uuid.uuid4(), name="Moodle", slug=f"moodle-{uuid.uuid4().hex[:8]}", platform_type="moodle",
        base_url=ISSUER, auth_type=IntegrationAuthType.lti13, tenant_id=TENANT, lti_issuer=ISSUER,
        lti_client_id="client-1", lti_deployment_id="dep-1",
        lti_auth_login_url=f"{ISSUER}/mod/lti/auth.php", lti_jwks_url=f"{ISSUER}/mod/lti/certs.php")
    db_session.add(platform)
    student = User(
        id=uuid.uuid4(), email="lms-7@example.test", display_name="lms-7", role=UserRole.student,
        tenant_id=TENANT, keycloak_id=f"lti:{platform.id}:lms-7", is_active=True)
    db_session.add(student)
    db_session.commit()
    token = lti_session.mint_session_token(db_session, student, platform, 600)
    return student, token


# ── Finding 1 — ws_auth.py:55: WebSocket handshake refuses LTI session tokens ─────────


@pytest.mark.xfail(strict=True, reason="audit #443: WebSocket handshake refuses LTI session tokens "
                                        "(control-plane/api/app/ws_auth.py:55)")
def test_ws_channel_accepts_an_lti_session_token(client, lti_world, db_session):
    """Audit #443 finding 1 (control-plane/api/app/ws_auth.py:55, medium): the WebSocket
    handshake refuses LTI session tokens — Moodle-launched Students cannot open any console
    or event stream.

    ws_user() validates the bearer subprotocol by calling
    auth.get_auth_backend().validate_token() directly (ws_auth.py:55) instead of resolving
    the token as auth.get_token_identity() does (auth.py:124-128), where the LTI session
    branch lives. The same token signs the Student in over HTTP (tests/api/
    test_lti_handoff.py::TestSessionToken), so the event stream must accept it too: the
    socket opens and answers on its tenant channel."""
    student, token = lti_world
    # The token is a valid LTI session by the HTTP path's own verifier — only ws_user refuses it.
    claims = lti_session.verify(db_session, token)
    assert claims["sub"] == student.keycloak_id and claims["tn_session"] == "lti"
    try:
        with client.websocket_connect(f"/ws/tenant.{TENANT}", subprotocols=["bearer", token]) as ws:
            # The handshake completed and the receive loop is live: a room action on a channel
            # the user may not join is answered, not met with a 1008 close.
            ws.send_json({"action": "room_members", "room_id": "not-a-room"})
            assert ws.receive_json() == {"type": "error", "detail": "room not allowed"}
    except WebSocketDisconnect as e:
        pytest.fail(f"the handshake refused the LTI session token: closed {e.code} {e.reason!r}; "
                    f"the same token signs the Student in over HTTP")
    assert student.keycloak_id.startswith("lti:")


# ── Finding 2 — ws_auth.py:55: a console grant the relay can never accept ─────────────


@pytest.mark.xfail(strict=True, reason="audit #443: an LTI-session Student can be issued a console "
                                        "grant over HTTP that the relay WebSocket can never accept "
                                        "(control-plane/api/app/ws_auth.py:55)")
def test_the_console_grant_an_lti_student_gets_opens(client, lti_world, db_session, monkeypatch):
    """Audit #443 finding 2 (control-plane/api/app/ws_auth.py:55, medium): an LTI-session
    Student can be issued a console grant over HTTP that the relay WebSocket can never
    accept.

    POST /lab-sessions/{id}/console issues a grant because it authenticates through
    get_current_user, which accepts the LTI session token; the relay's ws_user asks only the
    IdP backend (ws_auth.py:55), which refuses that token, and closes 1008 — the code the
    SPA renders as a permission problem. The grant the HTTP side issues must open."""
    student, token = lti_world
    tpl = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=TENANT)
    db_session.add(tpl)
    db_session.flush()
    vm = {"vm_id": "vm-1", "name": "abcdef12-ws1", "node_id": "ws1", "status": "running"}
    rng = Range(id=uuid.uuid4(), name="r", template_id=tpl.id, tenant_id=TENANT, state=RangeState.ready,
                provisioner_backend="mock", provisioner_output=json.dumps({"vms": [vm]}))
    db_session.add(rng)
    db_session.commit()
    monkeypatch.setattr(policy, "check", lambda db, grant, user: policy.Verdict(True))

    # HTTP: the dependency behind the console endpoints accepts the token ...
    from fastapi.security import HTTPAuthorizationCredentials

    ident = asyncio.run(auth.get_token_identity(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), db_session))
    assert ident.sub == student.keycloak_id

    # ... so the console it names must open with the same token, not close 1008.
    access = service.issue(db_session, MockConsole(), user_id=student.id, tenant_id=TENANT, rng=rng, vm=vm,
                           view_only=False, observed=False, base="wss://tn.test")
    db_session.commit()
    grant = access["url"].rsplit("/", 1)[1]
    from app.console_backends.mock import RFB_GREETING

    try:
        with client.websocket_connect(f"/ws/console/{grant}", subprotocols=["bearer", token]) as ws:
            assert ws.receive_bytes() == RFB_GREETING
    except WebSocketDisconnect as e:
        pytest.fail(f"the relay refused the grant's own user: closed {e.code} {e.reason!r}; "
                    f"the grant was issued to this very session over HTTP")


# ── Finding 3 — ws_auth.py:59: an IdP outage is reported as "not allowed" ─────────────


@pytest.mark.xfail(strict=True, reason="audit #443: a Keycloak outage with a cold JWKS cache is "
                                        "reported to WebSocket clients as 'not allowed' (1008) "
                                        "instead of 'service unavailable' (control-plane/api/app/ws_auth.py:59)")
def test_an_idp_outage_is_not_reported_as_not_allowed(client, monkeypatch):
    """Audit #443 finding 3 (control-plane/api/app/ws_auth.py:59, low): a Keycloak outage
    with a cold JWKS cache is reported to WebSocket clients as 'not allowed' (1008) instead
    of 'service unavailable'.

    On the HTTP path a cold JWKS fetch failure surfaces as 503
    (auth_backends/keycloak_oidc.py:66-72, generic_oidc.py:63-68); ws_user's bare
    ``except Exception: return None`` (ws_auth.py:59-60) turns the very same failure into a
    1008 policy refusal — the code the SPA renders as revoked access. An unavailable
    identity provider is a retryable condition: the socket must close 1013, not 1008."""
    from fastapi import HTTPException

    class IdpDown:
        async def validate_token(self, token):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "Authentication service temporarily unavailable")

    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "get_auth_backend", lambda: IdpDown())
    with pytest.raises(WebSocketDisconnect) as e, client.websocket_connect(
            f"/ws/tenant.{TENANT}", subprotocols=["bearer", "good-kc-a"]):
        pass
    assert e.value.code == 1013, f"an IdP outage closed as {e.value.code}, not 1013"


# ── Finding 4 — rbac.py:413: require_tenant_access cannot see the path parameter ──────


@pytest.mark.xfail(strict=True, reason="audit #443: require_tenant_access cannot see the path "
                                        "parameter it checks; it passes every cross-tenant request "
                                        "if ever used (control-plane/api/app/rbac.py:413)")
def test_require_tenant_access_rejects_a_cross_tenant_request():
    """Audit #443 finding 4 (control-plane/api/app/rbac.py:413, low): require_tenant_access
    cannot see the path parameter it checks; it passes every cross-tenant request if ever
    used.

    The dependency closure declares ``**kwargs: Any`` (rbac.py:404) and reads
    ``kwargs.get(tenant_id_param)`` (rbac.py:413); FastAPI resolves dependency arguments
    from declared parameters only, so target_tenant_id is always None and the 403 branch
    never fires. Wired exactly as its own docstring (rbac.py:390-398) shows, a tenant-B
    instructor's request for tenant A's data must be refused 403."""
    from fastapi import Depends, FastAPI

    mine = "22222222-2222-2222-2222-222222222222"
    other = "11111111-1111-1111-1111-111111111111"
    user = auth.CurrentUser(id="u1", email="i@example.test", display_name="i", role=UserRole.instructor,
                            tenant_id=mine, keycloak_id="kc-i")

    def _db():
        yield object()  # the instructor is no platform admin: is_platform_admin never reads it

    app = FastAPI()

    @app.get("/tenants/{tenant_id}/users")
    async def route(tenant_id: uuid.UUID, user: auth.CurrentUser = Depends(rbac.require_tenant_access("tenant_id"))):
        return {"ok": True}

    app.dependency_overrides[auth.get_current_user] = lambda: user
    app.dependency_overrides[rbac.get_db] = _db
    with TestClient(app) as c:
        r = c.get(f"/tenants/{other}/users")
    assert r.status_code == status.HTTP_403_FORBIDDEN, \
        f"expected 403 for a cross-tenant request, got {r.status_code}: {r.text}"
