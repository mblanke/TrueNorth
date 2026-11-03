"""The console relay: ``/ws/console/{grant}`` (app/console_relay, #366).

A browser reaches a VM's screen only through the API. The grant is the user's permission to
open one VM, five minutes, one socket at a time; the socket is authenticated as every other
(``ws_auth``, the ``bearer`` subprotocol). The mock adapter dials an in-process RFB stub, so
these tests run the real pump, close codes and single-socket rule. Who may be *issued* a
grant (per role, Student's own seat, observers) is in tests/api/test_class_consoles_321.py.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app import auth
from app.console_backends import GRANT_SECONDS, MockConsole
from app.console_backends.mock import RFB_GREETING
from app.console_relay import router as relay_router
from app.console_relay import service
from app.console_relay.models import ConsoleGrant
from app.models import Range, RangeState, Template, Tenant, User, UserRole
from starlette.websockets import WebSocketDisconnect

TENANT_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
BASE = "wss://tn.test"


def _tenant(db, tid):
    if not db.get(Tenant, tid):
        db.add(Tenant(id=tid, name=f"t-{tid.hex[-4:]}", slug=f"t-{tid.hex[-4:]}"))
        db.flush()


def _user(db, tid, kc, role=UserRole.student) -> User:
    _tenant(db, tid)
    u = User(id=uuid.uuid4(), email=f"{kc}@example.test", display_name=kc, role=role, tenant_id=tid,
             keycloak_id=kc, is_active=True)
    db.add(u)
    db.flush()
    return u


class World:
    def __init__(self, db):
        self.db = db
        self.ann = _user(db, TENANT_A, "kc-ann")
        self.bob = _user(db, TENANT_A, "kc-bob")
        self.lead = _user(db, TENANT_A, "kc-lead", UserRole.instructor)
        self.eve = _user(db, TENANT_B, "kc-eve")  # another tenant
        tpl = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=TENANT_A)
        db.add(tpl)
        db.flush()
        self.vm = {"vm_id": "vm-1", "name": "abcdef12-ws1", "node_id": "ws1", "status": "running"}
        self.rng = Range(id=uuid.uuid4(), name="r", template_id=tpl.id, tenant_id=TENANT_A, state=RangeState.ready,
                         provisioner_backend="mock", provisioner_output=json.dumps({"vms": [self.vm]}))
        db.add(self.rng)
        db.commit()

    def grant(self, user: User, **kw) -> str:
        """Issue a grant as the console endpoints do; returns the URL token."""
        access = service.issue(self.db, MockConsole(), user_id=user.id, tenant_id=TENANT_A, rng=self.rng,
                               vm=self.vm, view_only=kw.get("view_only", False), observed=False, base=BASE)
        self.db.commit()
        assert access["kind"] == "rfb-ws" and access["expires_in"] == GRANT_SECONDS
        assert access["url"].startswith(f"{BASE}/ws/console/")
        return access["url"].rsplit("/", 1)[1]


@pytest.fixture
def world(db_session, monkeypatch):
    """Real token checking, the identity provider stubbed: 'good-<kc>' is valid, signed in
    with a second factor; 'exp<seconds>-<kc>' is valid for that many seconds."""
    from fastapi import HTTPException

    class Backend:
        async def validate_token(self, token):
            if token.startswith("good-"):
                return {"sub": token[5:], "email": "x@example.test", "amr": ["pwd", "otp"]}
            if token.startswith("exp"):
                secs, kc = token[3:].split("-", 1)
                return {"sub": kc, "email": "x@example.test", "amr": ["pwd", "otp"], "exp": int(time.time()) + int(secs)}
            raise HTTPException(401, "bad token")

    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "get_auth_backend", lambda: Backend())
    MockConsole.dial_error = None
    MockConsole.dial_unreachable = False
    yield World(db_session)
    MockConsole.dial_error = None
    MockConsole.dial_unreachable = False


def _open(client, token, who="kc-ann", bearer=None):
    return client.websocket_connect(f"/ws/console/{token}", subprotocols=["bearer", bearer or f"good-{who}"])


def _closed_with(client, token, who="kc-ann", bearer=None) -> tuple[int, str]:
    with pytest.raises(WebSocketDisconnect) as e, _open(client, token, who, bearer) as ws:
        ws.receive_bytes()
    return e.value.code, e.value.reason


# ── The relay itself ─────────────────────────────────────────────────────────


def test_the_mock_relay_round_trips_rfb_bytes_and_frees_the_grant_on_close(client, world, db_session):
    token = world.grant(world.ann)
    with _open(client, token) as ws:
        assert ws.accepted_subprotocol == "bearer"
        assert ws.receive_bytes() == RFB_GREETING  # the upstream's greeting reaches the browser
        ws.send_bytes(b"RFB 003.008\n")  # the browser's answer reaches the upstream, echoed
        assert ws.receive_bytes() == b"RFB 003.008\n"
        ws.send_bytes(bytes(range(256)))
        assert ws.receive_bytes() == bytes(range(256))
    grant = db_session.query(ConsoleGrant).one()
    deadline = time.monotonic() + 5  # the relay gives the grant back after the browser has gone
    while time.monotonic() < deadline and (db_session.refresh(grant) or grant.live_until is not None):
        time.sleep(0.05)
    assert grant.live_until is None and grant.live_by is None
    with _open(client, token) as again:  # within its five minutes a closed grant opens again
        assert again.receive_bytes() == RFB_GREETING


def test_closing_the_browser_socket_closes_the_hypervisor_connection(client, world, monkeypatch):
    seen = []
    real = relay_router._upstream_connect

    async def spy(target):
        conn = await real(target)
        seen.append(conn)
        return conn

    monkeypatch.setattr(relay_router, "_upstream_connect", spy)
    with _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
    deadline = time.monotonic() + 5  # the relay finishes after the browser has gone
    while time.monotonic() < deadline and seen[0].state.name != "CLOSED":
        time.sleep(0.05)
    assert len(seen) == 1 and seen[0].state.name == "CLOSED"


def test_the_upstream_closing_cleanly_closes_the_browser_with_1000(client, world, monkeypatch):
    class Gone:
        async def send(self, data):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def close(self):
            pass

    async def connect(target):
        return Gone()

    monkeypatch.setattr(relay_router, "_upstream_connect", connect)
    assert _closed_with(client, world.grant(world.ann))[0] == 1000


def test_the_upstream_dropping_closes_the_browser_with_4503(client, world, monkeypatch):
    from websockets.exceptions import ConnectionClosedError

    class Dropped:
        async def send(self, data):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ConnectionClosedError(None, None)

        async def close(self):
            pass

    async def connect(target):
        return Dropped()

    monkeypatch.setattr(relay_router, "_upstream_connect", connect)
    assert _closed_with(client, world.grant(world.ann))[0] == 4503


# ── Who may open a grant ─────────────────────────────────────────────────────


def test_only_the_grants_own_user_may_open_it(client, world):
    token = world.grant(world.ann)
    assert _closed_with(client, token, "kc-bob")[0] == 1008  # another Student of the tenant
    assert _closed_with(client, token, "kc-lead")[0] == 1008  # staff of the tenant are not the grant's user
    assert _closed_with(client, token, "kc-eve")[0] == 1008  # another tenant's user
    with _open(client, token) as ws:  # none of that spent the grant
        assert ws.receive_bytes() == RFB_GREETING


def test_a_foreign_tenants_grant_is_refused_even_for_the_same_user_id(client, world, db_session):
    token = world.grant(world.ann)
    grant = db_session.query(ConsoleGrant).one()
    grant.tenant_id = TENANT_B  # a grant row that disagrees with the user's tenant
    db_session.commit()
    assert _closed_with(client, token)[0] == 1008


def test_no_sign_in_an_unknown_grant_or_a_bad_token_get_nothing(client, world):
    token = world.grant(world.ann)
    with pytest.raises(WebSocketDisconnect) as e, client.websocket_connect(f"/ws/console/{token}"):
        pass
    assert e.value.code == 1008
    assert _closed_with(client, token, bearer="forged")[0] == 1008
    assert _closed_with(client, "not-a-grant")[0] == 1008
    assert _closed_with(client, token[:-1] + ("A" if token[-1] != "A" else "B"))[0] == 1008


def test_the_grant_id_is_unguessable_and_only_its_hash_is_stored(world, db_session):
    token = world.grant(world.ann)
    assert len(token) >= 43  # token_urlsafe(32): 256 bits
    row = db_session.query(ConsoleGrant).one()
    assert row.token_hash == service.token_hash(token) and token not in {row.token_hash, str(row.id)}
    assert row.user_id == world.ann.id and row.tenant_id == TENANT_A and row.range_id == world.rng.id
    assert (row.vm_id, row.vm_name) == ("vm-1", "abcdef12-ws1")
    ttl = row.expires_at.replace(tzinfo=UTC) - row.created_at.replace(tzinfo=UTC)
    assert timedelta(minutes=4) <= ttl <= timedelta(minutes=6)


def test_view_only_is_carried_on_the_grant(world, db_session):
    world.grant(world.ann, view_only=True)
    assert db_session.query(ConsoleGrant).one().view_only is True


# ── Expiry and the one live socket ───────────────────────────────────────────


def test_an_expired_grant_closes_4410_for_its_user_only(client, world, db_session):
    token = world.grant(world.ann)
    grant = db_session.query(ConsoleGrant).one()
    grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    code, reason = _closed_with(client, token)
    assert code == 4410 and "expired" in reason
    assert _closed_with(client, token, "kc-bob")[0] == 1008  # the state of a grant is not told to others


def test_a_second_socket_on_a_live_grant_is_refused_4409(client, world):
    token = world.grant(world.ann)
    with _open(client, token) as first:
        assert first.receive_bytes() == RFB_GREETING
        code, reason = _closed_with(client, token)
        assert code == 4409 and "already open" in reason
        first.send_bytes(b"still mine")  # the first socket is untouched
        assert first.receive_bytes() == b"still mine"
    with _open(client, token) as after:  # and the grant is free again once it closed
        assert after.receive_bytes() == RFB_GREETING


def test_a_claim_whose_relay_died_lapses_and_the_grant_opens_again(client, world, db_session):
    token = world.grant(world.ann)
    db_session.query(ConsoleGrant).one().live_until = datetime.now(UTC) + timedelta(seconds=20)
    db_session.commit()
    assert _closed_with(client, token)[0] == 4409
    db_session.query(ConsoleGrant).one().live_until = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    with _open(client, token) as ws:
        assert ws.receive_bytes() == RFB_GREETING


def test_the_socket_closes_1001_when_the_sign_in_expires(client, world):
    token = world.grant(world.ann)
    with pytest.raises(WebSocketDisconnect) as e, _open(client, token, bearer="exp1-kc-ann") as ws:
        ws.receive_bytes()  # the greeting
        ws.receive_bytes()  # nothing more is sent: the close comes with the token's expiry
    assert e.value.code == 1001


# ── The machine and the hypervisor ───────────────────────────────────────────


def test_a_range_that_is_not_running_or_is_gone_closes_4404(client, world, db_session):
    token = world.grant(world.ann)
    world.rng.state = RangeState.stopped
    db_session.commit()
    assert _closed_with(client, token)[0] == 4404
    world.rng.state = RangeState.ready
    world.rng.deleted_at = datetime.now(UTC)
    db_session.commit()
    assert _closed_with(client, token)[0] == 4404


def test_a_hypervisor_that_refuses_the_console_closes_4502_with_its_reason(client, world):
    MockConsole.dial_error = "vCenter refused the console request"
    code, reason = _closed_with(client, world.grant(world.ann))
    assert code == 4502 and reason == "vCenter refused the console request"


def test_an_unreachable_hypervisor_closes_4502_without_the_address(client, world):
    MockConsole.dial_unreachable = True
    code, reason = _closed_with(client, world.grant(world.ann))
    assert code == 4502 and "127.0.0.1" not in reason and "ticket" not in reason


def test_a_hypervisor_that_does_not_answer_closes_4504(client, world, monkeypatch):
    async def never(target):
        raise TimeoutError

    monkeypatch.setattr(relay_router, "_upstream_connect", never)
    assert _closed_with(client, world.grant(world.ann))[0] == 4504


def test_a_failed_dial_frees_the_grant(client, world, db_session):
    token = world.grant(world.ann)
    MockConsole.dial_error = "no"
    assert _closed_with(client, token)[0] == 4502
    MockConsole.dial_error = None
    with _open(client, token) as ws:
        assert ws.receive_bytes() == RFB_GREETING


# ── The public address ───────────────────────────────────────────────────────


def test_the_public_address_is_the_site_domain_else_the_request_host(monkeypatch):
    monkeypatch.delenv("DOMAIN", raising=False)
    assert service.ws_base({"host": "tn.corp.test"}) == "wss://tn.corp.test"
    assert service.ws_base({"host": "localhost:4200", "x-forwarded-proto": "http"}) == "ws://localhost:4200"
    monkeypatch.setenv("DOMAIN", "range.corp.test")
    assert service.ws_base({"host": "attacker.test"}) == "wss://range.corp.test"
