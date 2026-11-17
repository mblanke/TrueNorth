"""A ``typ: lti-session`` token opens no WebSocket (row 13 of the #435 inventory, "ws-rejects-lti").

The LTI hand-off mints RS256 session tokens that ``auth.get_token_identity`` accepts on every
HTTP route (app/auth.py) — it is the only credential an LTI-launched Student holds. Both
WebSocket doors authenticate through ``ws_auth.ws_user`` (app/ws_auth.py:55), which validates
only against the identity provider backend, so that token kind is refused everywhere over
WebSocket: the collaboration channel handshake and the console relay, even for a grant issued
to that very Student. Pinned here is the refusal code those doors answer with today (1008,
pre-accept) — the limitation the inventory records and #443 filed against ws_auth.py:55.
"""

from __future__ import annotations

import json
import uuid

import pytest
from app import auth
from app.console_backends import MockConsole
from app.console_relay import policy, service
from app.lti_identity import session as lti_session
from app.models import ExternalPlatform, IntegrationAuthType, Range, RangeState, Template, User, UserRole
from starlette.websockets import WebSocketDisconnect

TENANT_A = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
ISSUER = "https://moodle.example.test"
BASE = "wss://tn.test"


@pytest.fixture
def lti_session_token(db_session, monkeypatch):
    """Real token checking with the identity provider stubbed to refuse every token, plus a
    launched Student (account created by the launch: ``lti:<platform>:<sub>``) holding a
    genuine lti-session token, minted exactly the way /lti/session mints one."""
    from app.models import Tenant
    from fastapi import HTTPException

    class Backend:
        async def validate_token(self, token):
            raise HTTPException(401, "bad token")

    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "get_auth_backend", lambda: Backend())
    if not db_session.get(Tenant, TENANT_A):
        db_session.add(Tenant(id=TENANT_A, name="t", slug="lti-ws"))
        db_session.flush()
    platform = ExternalPlatform(
        id=uuid.uuid4(), name="Moodle", slug=f"moodle-{uuid.uuid4().hex[:6]}", platform_type="moodle",
        base_url=ISSUER, auth_type=IntegrationAuthType.lti13, tenant_id=TENANT_A, lti_issuer=ISSUER,
        lti_client_id="client-1", lti_deployment_id="dep-1", lti_auth_login_url=f"{ISSUER}/mod/lti/auth.php",
        lti_jwks_url=f"{ISSUER}/mod/lti/certs.php",
    )
    db_session.add(platform)
    db_session.flush()
    student = User(
        id=uuid.uuid4(), email="learner@lms.example.test", display_name="Learner", role=UserRole.student,
        tenant_id=TENANT_A, keycloak_id=f"lti:{platform.id}:lms-7", is_active=True,
    )
    db_session.add(student)
    db_session.commit()
    return lti_session.mint_session_token(db_session, student, platform, 600), student


@pytest.fixture
def relay_policy(monkeypatch):
    """The grant policy answers yes, so the only gate on the console socket is ws_auth."""
    monkeypatch.setattr(policy, "check", lambda db, grant, user: policy.Verdict(True))


def _grant(db, student) -> str:
    """A console grant issued to the LTI Student themselves, as the console endpoints do."""
    tpl = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=TENANT_A)
    db.add(tpl)
    db.flush()
    vm = {"vm_id": "vm-1", "name": "abcdef12-ws1", "node_id": "ws1", "status": "running"}
    rng = Range(
        id=uuid.uuid4(), name="r", template_id=tpl.id, tenant_id=TENANT_A, state=RangeState.ready,
        provisioner_backend="mock", provisioner_output=json.dumps({"vms": [vm]}),
    )
    db.add(rng)
    db.commit()
    access = service.issue(
        db, MockConsole(), user_id=student.id, tenant_id=TENANT_A, rng=rng, vm=vm, view_only=False,
        observed=False, base=BASE, audience="student",
    )
    db.commit()
    assert access["kind"] == "rfb-ws"
    return access["url"].rsplit("/", 1)[1]


def test_the_same_token_signs_in_over_http(client, lti_session_token):
    """Control: the token is genuinely valid today, so the refusals below are the WebSocket
    doors' doing and not a worthless token."""
    token = lti_session_token[0]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    assert me.json()["user"]["role"] == "student"


def test_an_lti_session_token_opens_no_channel_socket(client, lti_session_token):
    token, _ = lti_session_token
    with (
        pytest.raises(WebSocketDisconnect) as e,
        client.websocket_connect(f"/ws/tenant.{TENANT_A}", subprotocols=["bearer", token]),
    ):
        pass
    assert e.value.code == 1008


def test_an_lti_session_token_opens_no_console_not_even_its_own_grant(
    client, lti_session_token, relay_policy, db_session
):
    """The strongest form: the grant was issued to this very Student, and the token still
    never reaches the grant check — ws_auth refuses first, pre-accept, with 1008."""
    token, student = lti_session_token
    grant = _grant(db_session, student)
    with (
        pytest.raises(WebSocketDisconnect) as e,
        client.websocket_connect(f"/ws/console/{grant}", subprotocols=["bearer", token]),
    ):
        pass
    assert e.value.code == 1008
