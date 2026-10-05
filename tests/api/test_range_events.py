"""Range events on the WebSocket: only to the range's tenant, only to signed-in users.

The black-box test (test_range_events_blackbox.py) proves a worker's range state reaches
a socket at all. These pin who may receive it. Before this, ``/ws/{channel}`` accepted
anyone without a token and any channel name, so relaying range states to it as it was
would have shown every tenant's ranges to anyone who connected.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

import pytest
from app import auth, range_events
from app.models import Range, Template, Tenant, User, UserRole
from app.websocket_manager import WebSocketManager
from starlette.websockets import WebSocketDisconnect

TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")


class FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.subprotocol = None

    async def accept(self, subprotocol=None):
        self.subprotocol = subprotocol

    async def send_json(self, msg):
        self.sent.append(msg)

    async def close(self, *a, **k):
        pass


def _tenant(db, tid):
    if not db.get(Tenant, tid):
        db.add(Tenant(id=tid, name=f"t-{tid.hex[-4:]}", slug=f"t-{tid.hex[-4:]}"))
        db.flush()


def _range(db, tid) -> Range:
    _tenant(db, tid)
    tpl = Template(id=uuid.uuid4(), name="t", yaml="id: t\n", tenant_id=tid)
    db.add(tpl)
    db.flush()
    r = Range(id=uuid.uuid4(), name="r", template_id=tpl.id, tenant_id=tid)
    db.add(r)
    db.commit()
    return r


@contextmanager
def _sessions(db):
    yield db


async def _relay(manager, db, payload) -> int:
    return await range_events.relay(manager, json.dumps(payload), lambda: _sessions(db))


@pytest.mark.asyncio
async def test_a_range_event_reaches_only_its_tenant(db_session):
    rng = _range(db_session, TENANT_A)
    m = WebSocketManager()
    mine, mine_range, other, anon = FakeSocket(), FakeSocket(), FakeSocket(), FakeSocket()
    await m.connect(mine, "ranges", user_id="u1", tenant_id=str(TENANT_A))
    await m.connect(mine_range, f"range.{rng.id}", user_id="u2", tenant_id=str(TENANT_A))
    await m.connect(other, "ranges", user_id="u3", tenant_id=str(TENANT_B))
    await m.connect(anon, "ranges")
    sent = await _relay(m, db_session, {"id": str(rng.id), "state": "ready", "error": None, "secret": "x"})
    assert sent == 2
    for sock in (mine, mine_range):
        (msg,) = sock.sent
        assert msg["type"] == "range_state"
        assert msg["data"] == {"id": str(rng.id), "state": "ready", "error": None}, "only the allow-listed fields"
    assert other.sent == [] and anon.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["not json", json.dumps({"state": "ready"}), json.dumps({"id": "nope"})])
async def test_malformed_or_unknown_events_are_dropped(db_session, payload):
    m = WebSocketManager()
    sock = FakeSocket()
    await m.connect(sock, "ranges", user_id="u1", tenant_id=str(TENANT_A))
    assert await range_events.relay(m, payload, lambda: _sessions(db_session)) == 0
    assert await _relay(m, db_session, {"id": str(uuid.uuid4()), "state": "ready"}) == 0
    assert sock.sent == []


# ── The socket itself ──────────────────────────────────────────────────
@pytest.fixture
def auth_on(monkeypatch, db_session):
    """Real token checking, with the identity provider stubbed: token 'good-<kc>' is valid."""
    from fastapi import HTTPException

    class Backend:
        async def validate_token(self, token):
            if not token.startswith("good-"):
                raise HTTPException(401, "bad token")
            return {"sub": token[len("good-") :], "email": "x@example.test"}

    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "get_auth_backend", lambda: Backend())
    _tenant(db_session, TENANT_A)
    db_session.add(
        User(
            id=uuid.uuid4(),
            email="i@example.test",
            display_name="i",
            role=UserRole.instructor,
            tenant_id=TENANT_A,
            keycloak_id="kc-a",
            is_active=True,
        )
    )
    db_session.commit()


@pytest.mark.parametrize("protocols", [None, ["bearer", "bad-token"], ["bearer", "good-kc-unknown"]])
def test_no_valid_token_no_socket(client, auth_on, protocols):
    kwargs = {"subprotocols": protocols} if protocols else {}
    with pytest.raises(WebSocketDisconnect) as e, client.websocket_connect("/ws/ranges", **kwargs):
        pass
    assert e.value.code == 1008


def test_a_signed_in_user_connects_and_heartbeats(client, auth_on):
    with client.websocket_connect("/ws/ranges", subprotocols=["bearer", "good-kc-a"]) as ws:
        assert ws.accepted_subprotocol == "bearer"
        ws.send_text(json.dumps({"type": "pong"}))
        ws.send_text(json.dumps({"hello": 1}))
        assert ws.receive_json()["type"] == "ack"


def test_another_tenants_range_channel_is_refused(client, auth_on, db_session):
    theirs = _range(db_session, TENANT_B)
    mine = _range(db_session, TENANT_A)
    with (
        pytest.raises(WebSocketDisconnect) as e,
        client.websocket_connect(f"/ws/range.{theirs.id}", subprotocols=["bearer", "good-kc-a"]),
    ):
        pass
    assert e.value.code == 1008
    with client.websocket_connect(f"/ws/range.{mine.id}", subprotocols=["bearer", "good-kc-a"]) as ws:
        assert ws.accepted_subprotocol == "bearer"


def test_admin_only_channels_stay_admin_only(client, auth_on):
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/system.alerts", subprotocols=["bearer", "good-kc-a"]),
    ):
        pass
