"""Who may open /ws/{channel} (app/ws_auth.py, CR1-07 in docs/review/codereview1.md).

On main the endpoint accepted anyone without a token, on any channel name, and let any
socket join, list or post into any collaboration room. Re-landed from #32 without its
range-event relay: channels with no tenant (ranges, exercises, all) stay closed.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from app import auth
from app.models import Range, Template, Tenant, User, UserRole
from app.websocket_manager import WebSocketManager
from app.ws_auth import room_message_type
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
    with pytest.raises(WebSocketDisconnect) as e, client.websocket_connect(f"/ws/tenant.{TENANT_A}", **kwargs):
        pass
    assert e.value.code == 1008


def test_a_signed_in_user_connects_and_heartbeats(client, auth_on):
    with client.websocket_connect(f"/ws/tenant.{TENANT_A}", subprotocols=["bearer", "good-kc-a"]) as ws:
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


# ── From the security review of 1b7618e ────────────────────────────────
def _exercise(db, tid):
    from app.models import Exercise, Scenario

    rng = _range(db, tid)
    sc = Scenario(id=uuid.uuid4(), name="s", yaml="id: s\n", tenant_id=tid)
    db.add(sc)
    db.flush()
    ex = Exercise(id=uuid.uuid4(), name="e", range_id=rng.id, scenario_id=sc.id, tenant_id=tid)
    db.add(ex)
    db.commit()
    return ex


@pytest.fixture
def user_b(auth_on, db_session):
    _tenant(db_session, TENANT_B)
    db_session.add(
        User(
            id=uuid.uuid4(),
            email="b@example.test",
            display_name="b",
            role=UserRole.instructor,
            tenant_id=TENANT_B,
            keycloak_id="kc-b",
            is_active=True,
        )
    )
    db_session.commit()


A = ["bearer", "good-kc-a"]
B = ["bearer", "good-kc-b"]


@pytest.mark.parametrize(
    "channel", ["ranges", "exercises", "all", "whatever", "tenant.00000000-0000-0000-0000-00000000000b"]
)
def test_channels_are_closed_by_default(client, auth_on, channel):
    with pytest.raises(WebSocketDisconnect) as e, client.websocket_connect(f"/ws/{channel}", subprotocols=A):
        pass
    assert e.value.code == 1008


def test_another_tenants_exercise_channel_is_refused(client, auth_on, db_session):
    """The ops center broadcasts injects and commands on exercise.<id>."""
    theirs = _exercise(db_session, TENANT_B)
    mine = _exercise(db_session, TENANT_A)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"/ws/exercise.{theirs.id}", subprotocols=A):
        pass
    with client.websocket_connect(f"/ws/exercise.{mine.id}", subprotocols=A) as ws:
        assert ws.accepted_subprotocol == "bearer"


def test_rooms_belong_to_a_tenants_exercise_and_need_joining(client, user_b, db_session):
    ex = _exercise(db_session, TENANT_A)
    with (
        client.websocket_connect(f"/ws/tenant.{TENANT_A}", subprotocols=A) as a,
        client.websocket_connect(f"/ws/tenant.{TENANT_B}", subprotocols=B) as b,
    ):
        a.send_text(json.dumps({"action": "join_room", "room_id": str(ex.id), "display_name": "A"}))
        assert a.receive_json()["type"] == "room_member_joined"
        # B cannot join A's room, list its members, or post into it without joining.
        for action in ("join_room", "room_members", "room_message"):
            b.send_text(json.dumps({"action": action, "room_id": str(ex.id), "type": "instructor_inject", "data": {}}))
            assert b.receive_json() == {"type": "error", "detail": "room not allowed"}
        # A's own member may post, but only room_* types go out.
        a.send_text(json.dumps({"action": "room_members", "room_id": str(ex.id)}))
        assert a.receive_json()["type"] == "room_members"


def test_a_room_message_type_is_always_a_room_type():
    assert room_message_type("room_chat") == "room_chat"
    assert room_message_type("room_cursor") == "room_cursor"
    assert room_message_type("instructor_inject") == "room_chat"
    assert room_message_type(None) == "room_chat"


@pytest.mark.asyncio
async def test_a_socket_closes_when_its_token_expires():
    m = WebSocketManager()
    live, expired, forever = FakeSocket(), FakeSocket(), FakeSocket()
    await m.connect(live, "ranges", user_id="u1", tenant_id="t", expires_at=time.time() + 600)
    await m.connect(expired, "ranges", user_id="u2", tenant_id="t", expires_at=time.time() - 1)
    await m.connect(forever, "ranges", user_id="u3", tenant_id="t")  # AUTH_DISABLED: no token
    assert await m.close_expired() == 1
    assert {c.user_id for c in m.connections.values()} == {"u1", "u3"}


# ── From the security review of 45066a2 ────────────────────────────────


def _user(db, kc: str, role: UserRole, tenant=TENANT_A):
    _tenant(db, tenant)
    db.add(
        User(
            id=uuid.uuid4(),
            email=f"{kc}@example.test",
            display_name=kc,
            role=role,
            tenant_id=tenant,
            keycloak_id=kc,
            is_active=True,
        )
    )
    db.commit()
    return ["bearer", f"good-{kc}"]


def test_the_api_starts_the_heartbeat_that_closes_expired_sockets(client):
    """Nothing started the manager: close_expired never ran outside the tests."""
    from app.main import app as fastapi_app

    manager = fastapi_app.state.ws_manager
    assert manager._running and manager._tasks, "the lifespan must start the heartbeat"


@pytest.mark.asyncio
async def test_the_heartbeat_closes_an_expired_socket(monkeypatch):
    import asyncio

    from app import websocket_manager

    monkeypatch.setattr(websocket_manager, "HEARTBEAT_INTERVAL_S", 0.05)
    m = WebSocketManager()
    expired = FakeSocket()
    await m.connect(expired, "tenant.t", user_id="u1", tenant_id="t", expires_at=time.time() - 1)
    await m.start_local()
    try:
        await asyncio.sleep(0.3)
    finally:
        await m.shutdown()
    assert m.connections == {}


def test_a_room_member_can_post_only_chat_or_cursor_and_the_server_names_the_sender(client, auth_on, db_session):
    a2 = _user(db_session, "kc-a2", UserRole.student)
    ex = _exercise(db_session, TENANT_A)
    room = str(ex.id)
    with (
        client.websocket_connect(f"/ws/tenant.{TENANT_A}", subprotocols=A) as a,
        client.websocket_connect(f"/ws/tenant.{TENANT_A}", subprotocols=a2) as b,
    ):
        a.send_text(json.dumps({"action": "join_room", "room_id": room, "display_name": "A"}))
        assert a.receive_json()["type"] == "room_member_joined"
        b.send_text(json.dumps({"action": "join_room", "room_id": room, "display_name": {"x": 1}}))
        joined = a.receive_json()
        assert joined["data"]["display_name"] == "Anonymous", "a display name must be a string"
        b.receive_json()
        b.send_text(
            json.dumps(
                {
                    "action": "room_message",
                    "room_id": room,
                    "type": "instructor_inject",
                    "data": {"sender_user_id": "x"},
                }
            )
        )
        frame = a.receive_json()
        assert frame["type"] == "room_chat", "an inject (or any non-chat type) must not reach a room"
        assert frame["data"]["sender_user_id"] != "x", "the server stamps the sender"


def test_an_admin_opens_system_channels(client, auth_on, db_session):
    admin = _user(db_session, "kc-admin", UserRole.admin)
    with client.websocket_connect("/ws/system.alerts", subprotocols=admin) as ws:
        assert ws.accepted_subprotocol == "bearer"


@pytest.mark.parametrize("action", ["room_message", "room_members", "leave_room"])
def test_a_same_tenant_socket_that_has_not_joined_cannot_use_the_room(client, auth_on, db_session, action):
    ex = _exercise(db_session, TENANT_A)
    with client.websocket_connect(f"/ws/tenant.{TENANT_A}", subprotocols=A) as ws:
        ws.send_text(json.dumps({"action": action, "room_id": str(ex.id), "type": "room_chat", "data": {}}))
        ws.send_text("probe")  # always answered with an ack: a missing refusal fails, never hangs
        assert ws.receive_json() == {"type": "error", "detail": "room not allowed"}


def test_another_tenant_cannot_leave_into_a_room(client, user_b, db_session):
    """leave_room broadcast room_member_left into any room, unchecked."""
    ex = _exercise(db_session, TENANT_A)
    with client.websocket_connect(f"/ws/tenant.{TENANT_B}", subprotocols=B) as b:
        b.send_text(json.dumps({"action": "leave_room", "room_id": str(ex.id)}))
        b.send_text("probe")  # always answered with an ack: a missing refusal fails, never hangs
        assert b.receive_json() == {"type": "error", "detail": "room not allowed"}


def test_a_soft_deleted_exercise_is_neither_a_channel_nor_a_room(client, auth_on, db_session):
    from datetime import UTC, datetime

    ex = _exercise(db_session, TENANT_A)
    ex.deleted_at = datetime.now(UTC)
    db_session.commit()
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"/ws/exercise.{ex.id}", subprotocols=A):
        pass
    with client.websocket_connect(f"/ws/tenant.{TENANT_A}", subprotocols=A) as ws:
        ws.send_text(json.dumps({"action": "join_room", "room_id": str(ex.id)}))
        assert ws.receive_json() == {"type": "error", "detail": "room not allowed"}


def test_only_the_canonical_id_names_a_channel_or_a_room(client, auth_on, db_session):
    """Upper case, bare hex and urn:uuid: forms each made a separate channel."""
    ex = _exercise(db_session, TENANT_A)
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(f"/ws/exercise.{str(ex.id).upper()}", subprotocols=A),
    ):
        pass
    with client.websocket_connect(f"/ws/tenant.{TENANT_A}", subprotocols=A) as ws:
        for alias in (str(ex.id).upper(), ex.id.hex, f"urn:uuid:{ex.id}"):
            ws.send_text(json.dumps({"action": "join_room", "room_id": alias}))
            assert ws.receive_json() == {"type": "error", "detail": "room not allowed"}


def test_a_students_lab_range_is_not_a_channel_for_another_student(client, auth_on, db_session, monkeypatch):
    from app.lab_sessions import service as labs

    rng = _range(db_session, TENANT_A)
    student = _user(db_session, "kc-s", UserRole.student)
    monkeypatch.setattr(labs, "lab_range_ids", lambda db, ids: set(ids))
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"/ws/range.{rng.id}", subprotocols=student):
        pass


def test_an_oversized_frame_is_refused(client, auth_on):
    with client.websocket_connect(f"/ws/tenant.{TENANT_A}", subprotocols=A) as ws:
        ws.send_text("x" * (64 * 1024 + 1))
        assert ws.receive_json() == {"type": "error", "detail": "frame too large"}
