"""Live push of notifications to the bell (issue #377).

A notification committed for someone is pushed to that person's own sockets, on the channel
``user.<their id>``. These tests pin who can hold that channel, that only the recipient
receives, that nothing is sent for work that did not commit, and that the relay between API
workers delivers across processes and never to its own origin.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from app import auth
from app.models import Tenant, User, UserRole
from app.notifications import inbox, push
from app.websocket_manager import WebSocketManager
from starlette.websockets import WebSocketDisconnect

TENANT = uuid.UUID("00000000-0000-0000-0000-00000000000a")
OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000000b")
DEV_ID = "00000000-0000-0000-0000-000000000001"  # AUTH_DISABLED's user (and tenant)


# ── Who may open user.<id> ─────────────────────────────────────────────
def test_a_user_opens_their_own_channel(client):
    with client.websocket_connect(f"/ws/user.{DEV_ID}") as ws:
        ws.send_text("probe")
        assert ws.receive_json()["type"] == "ack"


@pytest.mark.parametrize(
    "channel",
    [
        f"user.{uuid.uuid4()}",  # someone else
        "user.",
        "user.me",
        f"user.{DEV_ID.replace('-', '')}",
        f"user.{{{DEV_ID}}}",
        f"user.urn:uuid:{DEV_ID}",
        f"user.{DEV_ID}.extra",
    ],
)
def test_nobody_else_and_no_other_spelling_is_accepted(client, channel):
    with pytest.raises(WebSocketDisconnect) as e, client.websocket_connect(f"/ws/{channel}"):
        pass
    assert e.value.code == 1008


# ── Real sockets, two real users ───────────────────────────────────────
@pytest.fixture
def two_users(monkeypatch, db_session):
    """Real token checking: 'good-staff' (range ops) and 'good-student' of one tenant."""
    from fastapi import HTTPException

    class Backend:
        async def validate_token(self, token):
            if token.startswith("good-"):
                return {"sub": token[len("good-") :], "email": "x@example.test", "amr": ["pwd", "otp"]}
            raise HTTPException(401, "bad token")

    monkeypatch.setattr(auth, "AUTH_DISABLED", False)
    monkeypatch.setattr(auth, "get_auth_backend", lambda: Backend())
    if not db_session.get(Tenant, TENANT):
        db_session.add(Tenant(id=TENANT, name="t-a", slug="t-a"))
        db_session.flush()
    out = {}
    for kc, role in (("staff", UserRole.range_ops), ("student", UserRole.student), ("student2", UserRole.student)):
        u = User(
            id=uuid.uuid4(),
            email=f"{kc}@example.test",
            display_name=kc,
            role=role,
            tenant_id=TENANT,
            keycloak_id=kc,
            is_active=True,
        )
        db_session.add(u)
        out[kc] = u
    db_session.commit()
    return out


@pytest.mark.parametrize(
    "spell", [str.upper, lambda i: i.replace("-", ""), lambda i: f"{{{i}}}", lambda i: f"urn:uuid:{i}"]
)
def test_only_the_canonical_spelling_of_their_own_id_opens(client, two_users, spell):
    """Each spelling would be a separate channel name: a socket on one never hears the other."""
    staff = two_users["staff"]
    with (
        pytest.raises(WebSocketDisconnect) as e,
        client.websocket_connect(f"/ws/user.{spell(str(staff.id))}", subprotocols=_tok("staff")),
    ):
        pass
    assert e.value.code == 1008


def test_a_signed_in_user_cannot_open_another_users_channel(client, two_users):
    staff, student = two_users["staff"], two_users["student"]
    with (
        pytest.raises(WebSocketDisconnect) as e,
        client.websocket_connect(f"/ws/user.{staff.id}", subprotocols=_tok("student")),
    ):
        pass
    assert e.value.code == 1008
    with client.websocket_connect(f"/ws/user.{student.id}", subprotocols=_tok("student")) as ws:
        ws.send_text("probe")
        assert ws.receive_json()["type"] == "ack"


def _tok(kc):
    return ["bearer", f"good-{kc}"]


def _http(kc):
    return {"Authorization": f"Bearer good-{kc}"}


def test_a_new_ticket_is_pushed_to_staff_and_to_nobody_else(client, two_users):
    staff, student, other = two_users["staff"], two_users["student"], two_users["student2"]
    with (
        client.websocket_connect(f"/ws/user.{staff.id}", subprotocols=_tok("staff")) as staff_ws,
        client.websocket_connect(f"/ws/user.{student.id}", subprotocols=_tok("student")) as student_ws,
        client.websocket_connect(f"/ws/user.{other.id}", subprotocols=_tok("student2")) as other_ws,
    ):
        r = client.post("/tickets", json={"subject": "dc01 will not boot"}, headers=_http("student"))
        assert r.status_code == 201, r.text
        key = r.json()["key"]

        frame = staff_ws.receive_json()
        assert frame["type"] == "notification"
        assert frame["channel"] == f"user.{staff.id}"
        data = frame["data"]
        assert data["title"] == f"{key}: new ticket"
        assert data["link"] == f"/support/{r.json()['id']}"
        assert data["kind"] == "ticket_new"
        assert data["read"] is False

        # Those who were not told get the next frame we ask for, not a notification.
        for ws in (student_ws, other_ws):
            ws.send_text("probe")
            assert ws.receive_json()["type"] == "ack"

        # What was pushed is what the bell lists.
        listed = client.get("/notifications", headers=_http("staff")).json()
        assert [n["id"] for n in listed] == [data["id"]]
        assert {k: listed[0][k] for k in ("title", "message", "link", "kind", "read")} == {
            k: data[k] for k in ("title", "message", "link", "kind", "read")
        }


def test_a_staff_reply_is_pushed_to_the_reporter_alone(client, two_users):
    student, other = two_users["student"], two_users["student2"]
    t = client.post("/tickets", json={"subject": "VM slow"}, headers=_http("student")).json()
    with (
        client.websocket_connect(f"/ws/user.{student.id}", subprotocols=_tok("student")) as student_ws,
        client.websocket_connect(f"/ws/user.{other.id}", subprotocols=_tok("student2")) as other_ws,
    ):
        r = client.post(f"/tickets/{t['id']}/comments", json={"body": "Rebooting the host."}, headers=_http("staff"))
        assert r.status_code == 201, r.text
        frame = student_ws.receive_json()
        assert frame["type"] == "notification" and frame["data"]["kind"] == "ticket_reply"
        other_ws.send_text("probe")
        assert other_ws.receive_json()["type"] == "ack"


def test_an_internal_note_is_never_pushed_to_the_reporter(client, two_users):
    student = two_users["student"]
    t = client.post("/tickets", json={"subject": "x"}, headers=_http("student")).json()
    with client.websocket_connect(f"/ws/user.{student.id}", subprotocols=_tok("student")) as ws:
        r = client.post(
            f"/tickets/{t['id']}/comments", json={"body": "user error", "is_internal": True}, headers=_http("staff")
        )
        assert r.status_code == 201
        ws.send_text("probe")
        assert ws.receive_json()["type"] == "ack"


def test_every_socket_of_the_recipient_gets_it(client, two_users):
    staff = two_users["staff"]
    with (
        client.websocket_connect(f"/ws/user.{staff.id}", subprotocols=_tok("staff")) as one,
        client.websocket_connect(f"/ws/user.{staff.id}", subprotocols=_tok("staff")) as two,
    ):
        client.post("/tickets", json={"subject": "x"}, headers=_http("student"))
        assert one.receive_json()["type"] == "notification"
        assert two.receive_json()["type"] == "notification"


# ── Commit, rollback, savepoints ───────────────────────────────────────
@pytest.fixture
def sent(monkeypatch):
    out: list[tuple[str, dict]] = []
    monkeypatch.setattr(push, "_manager", object())
    monkeypatch.setattr(push, "_spawn", lambda manager, channel, payload: out.append((channel, payload)))
    return out


def _notify(db, who=DEV_ID, **kw):
    return inbox.notify(db, tenant_id=uuid.UUID(DEV_ID), user_ids=[uuid.UUID(who)], actor_id=None, title="t", **kw)


def test_nothing_is_pushed_before_the_commit(db_session, sent):
    _notify(db_session)
    db_session.flush()
    assert sent == []
    db_session.commit()
    assert [c for c, _ in sent] == [f"user.{DEV_ID}"]


def test_a_rolled_back_transaction_pushes_nothing(db_session, sent):
    sp = db_session.begin_nested()
    _notify(db_session)
    db_session.flush()
    sp.rollback()
    db_session.commit()
    assert sent == []


def test_a_notification_whose_savepoint_rolled_back_is_not_pushed(db_session, sent):
    _notify(db_session, link="/kept")
    sp = db_session.begin_nested()
    _notify(db_session, link="/discarded")
    db_session.flush()
    sp.rollback()
    db_session.commit()
    assert [p["link"] for _, p in sent] == ["/kept"]


def test_a_push_error_never_fails_the_commit(db_session, monkeypatch):
    monkeypatch.setattr(push, "_manager", object())

    def boom(*a, **k):
        raise RuntimeError("no loop")

    monkeypatch.setattr(push, "_spawn", boom)
    _notify(db_session)
    db_session.commit()  # does not raise


def test_without_a_manager_pushing_is_off(db_session, monkeypatch):
    monkeypatch.setattr(push, "_manager", None)
    called = []
    monkeypatch.setattr(push, "_spawn", lambda *a: called.append(a))
    _notify(db_session)
    db_session.commit()
    assert called == []


def test_the_actor_is_not_pushed_their_own_change(db_session, sent):
    inbox.notify(
        db_session,
        tenant_id=uuid.UUID(DEV_ID),
        user_ids=[uuid.UUID(DEV_ID)],
        actor_id=uuid.UUID(DEV_ID),
        title="mine",
    )
    db_session.commit()
    assert sent == []


# ── The relay between API workers ──────────────────────────────────────
class FakeBus:
    """One Redis shared by several managers: publish reaches every subscriber's queue."""

    def __init__(self):
        self.subscribers: list[asyncio.Queue] = []

    def pubsub(self):
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(q)
        return FakePubSub(q)

    async def publish(self, channel, payload):
        for q in self.subscribers:
            q.put_nowait({"type": "pmessage", "channel": channel, "data": payload})


class FakePubSub:
    def __init__(self, q):
        self.q = q

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        try:
            return await asyncio.wait_for(self.q.get(), timeout)
        except TimeoutError:
            return None


class FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def accept(self, subprotocol=None):
        pass

    async def send_json(self, msg):
        self.sent.append(msg)

    async def close(self, *a, **k):
        pass


async def _worker(bus):
    m = WebSocketManager()
    m._redis, m._pubsub, m._running = bus, bus.pubsub(), True
    m._tasks.append(asyncio.create_task(m._redis_listener()))
    return m


async def _settle():
    await asyncio.sleep(0.2)


@pytest.mark.asyncio
async def test_a_push_on_one_worker_reaches_a_socket_on_another():
    bus = FakeBus()
    a, b = await _worker(bus), await _worker(bus)
    sock_a, sock_b, stranger = FakeSocket(), FakeSocket(), FakeSocket()
    try:
        await b.connect(sock_b, "user.u1", user_id="u1", tenant_id="t")
        await b.connect(stranger, "user.u2", user_id="u2", tenant_id="t")
        await a.connect(sock_a, "user.u1", user_id="u1", tenant_id="t")  # same user, other worker

        await a.broadcast("user.u1", "notification", {"id": "n1"})
        await _settle()

        assert [m["data"] for m in sock_b.sent] == [{"id": "n1"}], "the other worker's socket must receive it"
        assert [m["data"] for m in sock_a.sent] == [{"id": "n1"}], "and the origin's own exactly once"
        assert stranger.sent == [], "never another user's"
    finally:
        for m in (a, b):
            await m.shutdown()


@pytest.mark.asyncio
async def test_a_worker_ignores_its_own_relayed_message_and_only_its_own():
    bus = FakeBus()
    a, b = await _worker(bus), await _worker(bus)
    assert a._origin != b._origin, "two workers must never share an origin (id(self) could collide)"
    sock = FakeSocket()
    try:
        await a.connect(sock, "user.u1", user_id="u1", tenant_id="t")

        def frame(origin):
            return json.dumps(
                {"channel": "user.u1", "message_type": "notification", "data": {"o": origin}, "origin": origin}
            )

        await bus.publish("truenorth:ws:user.u1", frame(a._origin))  # a's own echo: dropped
        await bus.publish("truenorth:ws:user.u1", frame(b._origin))  # a sibling's: delivered
        await _settle()
        assert [m["data"]["o"] for m in sock.sent] == [b._origin]
    finally:
        for m in (a, b):
            await m.shutdown()


def test_the_api_starts_with_the_relay_off_and_falls_back_when_redis_is_unreachable(monkeypatch):
    """WS_REDIS_RELAY on with an unreachable Redis: the API still starts, local-only."""
    from app.main import app as fastapi_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WS_REDIS_RELAY", "true")
    with TestClient(fastapi_app):
        manager = fastapi_app.state.ws_manager
        assert manager._running
        assert manager._redis is None, "an unreachable Redis means local delivery only"
        assert any("heartbeat" in repr(t) for t in manager._tasks), "the heartbeat still runs"


def test_the_spawn_is_a_noop_off_the_event_loop(monkeypatch):
    """A script or Celery task has no loop: pushing is skipped, not an error."""
    monkeypatch.setattr(push, "_manager", SimpleNamespace(broadcast=None))
    push.deliver([(SimpleNamespace(), "u", {})])  # not persistent either: skipped quietly
    push._spawn(SimpleNamespace(), "user.u", {})


# ── Against a real Redis ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_relay_between_two_workers_works_on_a_real_redis():
    """Two managers, each with its own Redis connection and listener, as two API workers
    have. The code had only ever run against fakes. Set TEST_REDIS_URL to run it."""
    import os

    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("set TEST_REDIS_URL to run against a real Redis")
    a, b = WebSocketManager(redis_url=url), WebSocketManager(redis_url=url)
    sock_a, sock_b, stranger = FakeSocket(), FakeSocket(), FakeSocket()
    await a.start(heartbeat=False)
    await b.start(heartbeat=False)
    try:
        assert a._redis is not None and b._redis is not None, "Redis must be reachable for this test"
        await a.connect(sock_a, "user.u1", user_id="u1", tenant_id="t")
        await b.connect(sock_b, "user.u1", user_id="u1", tenant_id="t")
        await b.connect(stranger, "user.u2", user_id="u2", tenant_id="t")
        await asyncio.sleep(0.3)  # both listeners subscribed

        await a.broadcast("user.u1", "notification", {"id": "n1"})
        await asyncio.sleep(0.5)

        assert [m["data"] for m in sock_b.sent] == [{"id": "n1"}]
        assert [m["data"] for m in sock_a.sent] == [{"id": "n1"}], "delivered locally once, not echoed back"
        assert stranger.sent == []
    finally:
        await a.shutdown()
        await b.shutdown()
