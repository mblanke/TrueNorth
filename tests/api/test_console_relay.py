"""The console relay: ``/ws/console/{grant}`` (app/console_relay, #366).

A browser reaches a VM's screen only through the API. The grant is the user's permission to
open one VM, five minutes, one socket at a time; the socket is authenticated as every other
(``ws_auth``, the ``bearer`` subprotocol). The mock adapter dials an in-process RFB stub, so
these tests run the real pump, close codes and single-socket rule. Who may be *issued* a
grant (per role, Student's own seat, observers) is in tests/api/test_class_consoles_321.py.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app import auth
from app.console_backends import GRANT_SECONDS, MockConsole
from app.console_backends.mock import RFB_GREETING
from app.console_relay import policy, service
from app.console_relay import router as relay_router
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


world_verdict = [policy.Verdict(True)]  # what the stubbed policy answers; tests change it


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
        vm = {**self.vm, "name": kw["vm_name"], "vm_id": kw["vm_name"]} if kw.get("vm_name") else self.vm
        access = service.issue(self.db, MockConsole(), user_id=user.id, tenant_id=TENANT_A, rng=self.rng,
                               vm=vm, view_only=kw.get("view_only", False), observed=False, base=BASE,
                               audience=kw.get("audience", "student"), class_id=kw.get("class_id"),
                               event_id=kw.get("event_id"), lab_session_id=kw.get("lab_session_id"))
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
    # The relay's mechanics here; who may keep a console open is policy.py's, tested with real
    # classes and lab sessions in test_class_consoles_321.py and below (``verdict``).
    monkeypatch.setattr(policy, "check", lambda db, grant, user: world_verdict[0])
    world_verdict[0] = policy.Verdict(True)
    monkeypatch.setattr(service, "RENEW_SECONDS", 0.1)
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


def _wait_for(condition, seconds=3.0):
    """Poll until ``condition()`` holds, up to a deadline: the relay finishes (releases the
    grant, closes the hypervisor side) a moment after the browser has gone."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def _released(db):
    def check():
        db.expire_all()
        return all(g.live_by is None and g.live_until is None for g in db.query(ConsoleGrant).all())

    return check


@pytest.fixture
def isolated_db(client, db_session):
    """Each relay step on a session of its own that rolls back to a savepoint, so a step that
    raises (as it does in production, where the session is its own transaction) does not undo
    the test's rows."""
    from app.db import get_db
    from app.main import app as fastapi_app
    from sqlalchemy.orm import sessionmaker

    def factory():
        s = sessionmaker(bind=db_session.get_bind(), join_transaction_mode="create_savepoint", autoflush=False,
                         expire_on_commit=False)()
        try:
            yield s
        finally:
            s.close()

    fastapi_app.dependency_overrides[get_db] = factory


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


def test_a_second_socket_on_a_live_grant_is_refused_4409(client, world, db_session):
    token = world.grant(world.ann)
    with _open(client, token) as first:
        assert first.receive_bytes() == RFB_GREETING
        code, reason = _closed_with(client, token)
        assert code == 4409 and "already open" in reason
        first.send_bytes(b"still mine")  # the first socket is untouched
        assert first.receive_bytes() == b"still mine"
    assert _wait_for(_released(db_session))  # the relay gives the grant back after the browser has gone
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


def test_a_deleted_range_closes_4502_at_the_dial(client, world, db_session):
    token = world.grant(world.ann)
    world.rng.deleted_at = datetime.now(UTC)
    db_session.commit()
    code, reason = _closed_with(client, token)
    assert code == 4502 and "not running" in reason


def test_a_hypervisor_that_refuses_the_console_closes_4502_with_its_reason(client, world, isolated_db):
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


def test_a_failed_dial_frees_the_grant(client, world, db_session, isolated_db):
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


# ── Re-authorisation, lifetime and abuse bounds ──────────────────────────────


def test_a_no_from_policy_at_connect_closes_with_its_code_and_reason(client, world):
    token = world.grant(world.ann)
    world_verdict[0] = policy.Verdict(False, 1008, "Your session is not open")
    assert _closed_with(client, token) == (1008, "Your session is not open")
    world_verdict[0] = policy.Verdict(False, 4404, "The machine is not running")
    assert _closed_with(client, token)[0] == 4404
    world_verdict[0] = policy.Verdict(True)
    with _open(client, token) as ws:  # a refusal frees the grant and spends no dial
        assert ws.receive_bytes() == RFB_GREETING


def test_a_no_from_policy_while_connected_closes_the_socket(client, world):
    with pytest.raises(WebSocketDisconnect) as e, _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        world_verdict[0] = policy.Verdict(False, 1008, "You can no longer open this console")
        ws.receive_bytes()  # the next renewal asks again
    assert e.value.code == 1008 and "no longer" in e.value.reason


def test_the_socket_ends_with_the_session_window(client, world):
    world_verdict[0] = policy.Verdict(True, until=time.time() + 0.5)
    with pytest.raises(WebSocketDisconnect) as e, _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        ws.receive_bytes()
    assert e.value.code == 1008 and "ended" in e.value.reason


def test_a_withdrawn_grant_closes_with_its_own_reason(client, world, db_session):
    with pytest.raises(WebSocketDisconnect) as e, _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        db_session.query(ConsoleGrant).delete()
        db_session.commit()
        ws.receive_bytes()
    assert e.value.code == 4404 and "withdrawn" in e.value.reason and "another window" not in e.value.reason


def test_a_grant_taken_by_another_socket_closes_4409(client, world, db_session):
    with pytest.raises(WebSocketDisconnect) as e, _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        row = db_session.query(ConsoleGrant).one()
        row.live_by = "someone-else"
        db_session.commit()
        ws.receive_bytes()
    assert e.value.code == 4409 and "another window" in e.value.reason


def test_an_idle_console_is_closed_4408(client, world, monkeypatch):
    monkeypatch.setattr(relay_router, "IDLE_SECONDS", 0.5)
    with pytest.raises(WebSocketDisconnect) as e, _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        ws.receive_bytes()
    assert e.value.code == 4408


def test_activity_keeps_a_console_from_idling(client, world, monkeypatch):
    # The idle limit is several times the gap between sends so a stalled CI runner
    # cannot make the activity look like silence.
    monkeypatch.setattr(relay_router, "IDLE_SECONDS", 3.0)
    with _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        for _ in range(4):
            time.sleep(0.5)
            ws.send_bytes(b"k")
            assert ws.receive_bytes() == b"k"


def test_a_message_from_the_browser_over_the_bound_closes_1009(client, world, monkeypatch):
    monkeypatch.setattr(relay_router, "MAX_CLIENT_MESSAGE", 1024)
    with pytest.raises(WebSocketDisconnect) as e, _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        ws.send_bytes(b"x" * 1025)
        ws.receive_bytes()
    assert e.value.code == 1009


def test_a_grant_opens_three_times_and_then_is_used_up(client, world, db_session):
    token = world.grant(world.ann)
    for _ in range(service.MAX_DIALS):
        with _open(client, token) as ws:
            assert ws.receive_bytes() == RFB_GREETING
        assert _wait_for(_released(db_session))  # the relay gives the grant back after the browser has gone
    code, reason = _closed_with(client, token)
    assert code == 4410 and "used up" in reason


def test_failed_dials_do_not_spend_a_grants_connections(client, world, isolated_db):
    token = world.grant(world.ann)
    MockConsole.dial_error = "no"
    for _ in range(4):
        assert _closed_with(client, token)[0] == 4502
    MockConsole.dial_error = None
    with _open(client, token) as ws:
        assert ws.receive_bytes() == RFB_GREETING


def test_a_user_holds_four_live_grants_at_most(world, db_session):
    for i in range(service.MAX_LIVE_GRANTS):
        world.grant(world.ann, vm_name=f"vm{i}")
    with pytest.raises(service.GrantRefusedError) as e:
        world.grant(world.ann, vm_name="vm-another")
    assert e.value.status == 429
    world.grant(world.bob)  # the bound is per user
    db_session.query(ConsoleGrant).filter(ConsoleGrant.user_id == world.ann.id).first().expires_at = (
        datetime.now(UTC) - timedelta(seconds=1))
    db_session.commit()
    world.grant(world.ann)  # an expired one no longer counts


def test_dials_are_rate_limited_per_user(client, world, db_session, monkeypatch):
    monkeypatch.setattr(service, "DIALS_PER_MINUTE", 2)
    service._DIAL_TIMES.clear()
    for _ in range(2):
        with _open(client, world.grant(world.ann)) as ws:
            ws.receive_bytes()
        assert _wait_for(_released(db_session))
    code, reason = _closed_with(client, world.grant(world.ann))
    assert code == 4429 and "wait" in reason
    with _open(client, world.grant(world.bob), "kc-bob") as ws:  # another user is not limited
        ws.receive_bytes()
    service._DIAL_TIMES.clear()


def test_issuing_deletes_only_the_issuers_own_lapsed_grants_never_a_held_one(world, db_session):
    old = datetime.now(UTC) - timedelta(days=2)
    world.grant(world.ann, vm_name="a")
    world.grant(world.bob)
    world.grant(world.ann, vm_name="b")
    held, bob_old, lapsed_ann = db_session.query(ConsoleGrant).order_by(ConsoleGrant.created_at).all()
    lapsed_ann.expires_at = old
    bob_old.expires_at = old  # Bob's, lapsed: not Ann's to sweep
    held.expires_at = old
    held.live_until = datetime.now(UTC) + timedelta(seconds=20)  # a socket still holds it
    db_session.commit()
    held_id, bob_id, lapsed_id = held.id, bob_old.id, lapsed_ann.id
    world.grant(world.ann, vm_name="c")
    left = {g.id for g in db_session.query(ConsoleGrant).all()}
    assert held_id in left and bob_id in left and lapsed_id not in left


def test_a_dial_that_outlasts_its_wait_closes_4504_and_is_logged_when_it_ends(client, world, monkeypatch, caplog):
    monkeypatch.setattr(relay_router, "DIAL_SECONDS", 0.2)
    real = MockConsole.dial

    def slow(self, grant, *, db=None, rng=None):
        time.sleep(0.6)
        return real(self, grant, db=db, rng=rng)

    monkeypatch.setattr(MockConsole, "dial", slow)
    with caplog.at_level("INFO", logger="truenorth.console_relay"):
        assert _closed_with(client, world.grant(world.ann))[0] == 4504
        time.sleep(0.8)
    assert "had given up on has finished" in caplog.text


# ── Grants: replace, count sockets, atomic claim ─────────────────────────────


def test_asking_again_for_a_console_keeps_the_older_urls_and_counts_one_console(world, db_session, client):
    first = world.grant(world.ann)
    tokens = [first] + [world.grant(world.ann) for _ in range(service.MAX_LIVE_GRANTS + 2)]  # two tabs, a double click
    assert db_session.query(ConsoleGrant).count() == len(tokens)  # nothing was deleted
    for vm in ("b", "c", "d"):  # three more consoles: four in all, and still fine
        world.grant(world.ann, vm_name=vm)
    with pytest.raises(service.GrantRefusedError):  # a fifth console is one too many
        world.grant(world.ann, vm_name="e")
    with _open(client, first) as ws:  # the first URL still opens
        assert ws.receive_bytes() == RFB_GREETING


def test_one_console_has_a_bound_on_its_own_grants(world):
    for _ in range(service.MAX_PER_CONSOLE):
        world.grant(world.ann)
    with pytest.raises(service.GrantRefusedError, match="several windows"):
        world.grant(world.ann)


def test_a_grant_for_another_audience_or_scope_is_another_console_and_is_never_replaced(world, db_session):
    eid, lab = uuid.uuid4(), uuid.uuid4()
    world.grant(world.ann)  # the Student's own
    world.grant(world.ann, audience="staff", class_id=uuid.uuid4(), event_id=eid)  # same VM name, staff
    world.grant(world.ann, event_id=uuid.uuid4())  # same VM, another session
    world.grant(world.ann, lab_session_id=lab)  # same VM, a lab
    assert db_session.query(ConsoleGrant).count() == 4
    with pytest.raises(service.GrantRefusedError):  # four distinct consoles are four consoles
        world.grant(world.ann, vm_name="elsewhere")


def test_a_grant_a_socket_holds_still_counts_and_is_not_replaced(world, db_session):
    world.grant(world.ann)
    row = db_session.query(ConsoleGrant).one()
    row.dials, row.live_until = service.MAX_DIALS, datetime.now(UTC) + timedelta(seconds=20)  # used up, still open
    db_session.commit()
    for i in range(service.MAX_LIVE_GRANTS - 1):
        world.grant(world.ann, vm_name=f"v{i}")
    with pytest.raises(service.GrantRefusedError):
        world.grant(world.ann, vm_name="one-more")
    assert db_session.query(ConsoleGrant).filter(ConsoleGrant.id == row.id).count() == 1


def test_a_claim_cannot_win_the_last_dial_twice(world, db_session):
    """The dials limit is in the claim's UPDATE: a claim that read dials=2 loses to a dial that
    was recorded between its read and its update (spent, not busy)."""
    from sqlalchemy import update

    token = world.grant(world.ann)
    user = auth_user(world.ann)
    real = db_session.execute

    def racing(stmt, *a, **k):
        db_session.execute = real  # one race only
        real(update(ConsoleGrant).values(dials=service.MAX_DIALS))
        return real(stmt, *a, **k)

    db_session.execute = racing
    try:
        claimed = service.claim(db_session, token, user)
    finally:
        db_session.execute = real
    assert claimed.outcome == service.SPENT
    assert service.claim(db_session, token, user).outcome == service.SPENT


def test_two_overlapping_claims_one_wins(world, db_session):
    token = world.grant(world.ann)
    user = auth_user(world.ann)
    first = service.claim(db_session, token, user)
    second = service.claim(db_session, token, user)
    assert (first.outcome, second.outcome) == (service.OK, service.BUSY)
    service.release(db_session, first.facts.id, first.holder)
    assert service.claim(db_session, token, user).outcome == service.OK


def test_empty_rate_keys_do_not_pile_up(monkeypatch):
    service._DIAL_TIMES.clear()
    assert service.dial_allowed("u1")
    service._DIAL_TIMES["u1"].clear()  # a minute passed
    assert service.dial_allowed("u2")
    assert "u1" not in service._DIAL_TIMES
    service._DIAL_TIMES.clear()


def auth_user(u):
    from app.auth import CurrentUser

    return CurrentUser(id=str(u.id), email=u.email, display_name=u.display_name, role=u.role,
                       tenant_id=str(u.tenant_id), keycloak_id=u.keycloak_id)


# ── The relay's own database use ─────────────────────────────────────────────


def test_closing_during_a_renewal_leaves_the_grant_free(client, world, db_session, monkeypatch):
    real = service.renew

    def slow(db, grant_id, holder):
        time.sleep(0.4)  # the browser closes while this tick is in its thread
        return real(db, grant_id, holder)

    monkeypatch.setattr(service, "renew", slow)
    with _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        time.sleep(0.2)  # inside the slow renewal
    assert _wait_for(_released(db_session))
    row = db_session.query(ConsoleGrant).one()
    assert row.live_by is None and row.live_until is None


def test_a_release_that_fails_once_is_retried(client, world, db_session, isolated_db, monkeypatch):
    real, calls = service.release, []

    def flaky(db, grant_id, holder):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("connection reset")
        return real(db, grant_id, holder)

    monkeypatch.setattr(service, "release", flaky)
    with _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
    assert _wait_for(lambda: len(calls) >= 2 and _released(db_session)())
    assert len(calls) == 2


def _renewals_failing(monkeypatch, fail_on):
    """``service.renew`` raising a pool timeout on the ticks ``fail_on(n)`` says (n from 1)."""
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    real, ticks = service.renew, []

    def renew(db, grant_id, holder):
        ticks.append(1)
        if fail_on(len(ticks)):
            raise PoolTimeout("QueuePool limit of size 25 overflow 50 reached")
        return real(db, grant_id, holder)

    monkeypatch.setattr(service, "renew", renew)
    return ticks


def test_one_failed_renewal_does_not_close_the_socket(client, world, isolated_db, monkeypatch, caplog):
    ticks = _renewals_failing(monkeypatch, lambda n: n == 2)
    with caplog.at_level("WARNING", logger="truenorth.console_relay"), _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        assert _wait_for(lambda: len(ticks) >= 5)  # past the failed tick and several good ones
        ws.send_bytes(b"still here")
        assert ws.receive_bytes() == b"still here"
    assert "renewal failed (1 in a row)" in caplog.text and "TimeoutError" in caplog.text


def test_two_failed_renewals_in_a_row_are_tolerated_and_the_count_resets(client, world, isolated_db, monkeypatch):
    ticks = _renewals_failing(monkeypatch, lambda n: n in (2, 3, 5, 6))
    with _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        assert _wait_for(lambda: len(ticks) >= 9)
        ws.send_bytes(b"x")
        assert ws.receive_bytes() == b"x"


def test_three_failed_renewals_in_a_row_close_1011(client, world, isolated_db, monkeypatch):
    _renewals_failing(monkeypatch, lambda n: n >= 2)
    with pytest.raises(WebSocketDisconnect) as e, _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        ws.receive_bytes()
    assert e.value.code == 1011 and e.value.reason == "The console service had a problem. Reconnect."


def test_a_lapsing_claim_closes_without_waiting_for_the_third_failure(client, world, isolated_db, monkeypatch):
    monkeypatch.setattr(service, "LIVE_LEASE", timedelta(seconds=0.35))  # three ticks of 0.1 s would outlive it
    _renewals_failing(monkeypatch, lambda n: n >= 2)
    with pytest.raises(WebSocketDisconnect) as e, _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        ws.receive_bytes()
    assert e.value.code == 1011


def test_a_policy_denial_closes_at_once_not_after_three_ticks(client, world, monkeypatch):
    ticks = _renewals_failing(monkeypatch, lambda n: False)
    with pytest.raises(WebSocketDisconnect) as e, _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        world_verdict[0] = policy.Verdict(False, 1008, "You can no longer open this console")
        ws.receive_bytes()
    assert e.value.code == 1008 and len(ticks) <= 3


def test_a_dial_whose_claim_lapsed_meanwhile_closes_4409(client, world, db_session, isolated_db, monkeypatch):
    real = service.record_dial

    def taken(db, grant_id, holder):
        from sqlalchemy import update

        db.execute(update(ConsoleGrant).values(live_by="someone-else"))  # another socket took the grant
        return real(db, grant_id, holder)

    monkeypatch.setattr(service, "record_dial", taken)
    code, reason = _closed_with(client, world.grant(world.ann))
    assert code == 4409 and "another window" in reason
    db_session.expire_all()
    assert db_session.query(ConsoleGrant).one().dials == 0  # nothing was counted


def test_a_step_that_raises_is_rolled_back_not_committed(client, world, db_session, isolated_db, monkeypatch):
    from sqlalchemy import update

    def writes_then_fails(db, grant_id, holder):
        db.execute(update(ConsoleGrant).values(dials=99))
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "renew", writes_then_fails)
    with pytest.raises(WebSocketDisconnect) as e, _open(client, world.grant(world.ann)) as ws:
        ws.receive_bytes()
        ws.receive_bytes()
    assert e.value.code == 1011
    assert _wait_for(_released(db_session))
    db_session.expire_all()
    assert db_session.query(ConsoleGrant).one().dials == 1  # the one real dial; the failed step's write is gone


# ── Dials: a hypervisor that does not answer ─────────────────────────────────


def test_a_dial_runs_on_the_relays_own_threads_not_the_servers_shared_pool(client, world, monkeypatch):
    seen = []
    real = MockConsole.dial

    def note(self, grant, *, db=None, rng=None):
        seen.append(threading.current_thread().name)
        return real(self, grant, db=db, rng=rng)

    monkeypatch.setattr(MockConsole, "dial", note)
    with _open(client, world.grant(world.ann)) as ws:
        assert ws.receive_bytes() == RFB_GREETING
    assert seen and all(name.startswith("tn-dial") for name in seen)
    assert relay_router._dial_pool._max_workers == relay_router.DIAL_WORKERS


def test_one_user_has_one_dial_out_at_a_time_until_it_really_ends(client, world, monkeypatch):
    """A hypervisor that is sitting on a login holds a thread: redialling must not stack more."""
    monkeypatch.setattr(relay_router, "DIAL_SECONDS", 0.2)
    release = threading.Event()
    real = MockConsole.dial

    def stuck(self, grant, *, db=None, rng=None):
        release.wait(5)
        return real(self, grant, db=db, rng=rng)

    monkeypatch.setattr(MockConsole, "dial", stuck)
    try:
        assert _closed_with(client, world.grant(world.ann))[0] == 4504  # gave up waiting; the thread is still out
        code, reason = _closed_with(client, world.grant(world.ann, vm_name="b"))
        assert code == 4429 and "still being made" in reason
        assert _closed_with(client, world.grant(world.bob), who="kc-bob")[0] == 4504  # another user is not blocked
    finally:
        release.set()
    assert _wait_for(lambda: not relay_router._dials_in_flight)
    monkeypatch.setattr(MockConsole, "dial", real)
    with _open(client, world.grant(world.ann, vm_name="c")) as ws:  # and Ann can dial again
        assert ws.receive_bytes() == RFB_GREETING


def test_a_dial_still_queued_when_the_relay_gave_up_does_nothing(monkeypatch):
    from app.console_backends import ConsoleError, DialGrant

    called = []
    monkeypatch.setattr(relay_router, "get_console_backend", lambda kind: called.append(kind))
    gone = threading.Event()
    gone.set()
    with pytest.raises(ConsoleError):
        relay_router._dial_blocking(None, DialGrant(id="g", range_id=str(uuid.uuid4()), vm_id="v", vm_name="n"),
                                    TENANT_A, gone)
    assert called == []


# ── View-only is never claimed on a console that types ───────────────────────


def test_a_view_only_grant_is_not_served_from_a_console_that_accepts_input(client, world, db_session):
    MockConsole.supports_view_only = False
    code, reason = _closed_with(client, world.grant(world.ann, view_only=True))
    assert code == 4502 and "view-only" in reason
    assert _wait_for(_released(db_session))  # the grant was given back, not held


def test_a_view_only_grant_on_a_console_the_hypervisor_makes_read_only_drops_input(client, world, monkeypatch):
    monkeypatch.setattr(relay_router, "IDLE_SECONDS", 0.6)
    MockConsole.supports_view_only = True
    try:
        with pytest.raises(WebSocketDisconnect) as e, _open(client, world.grant(world.ann, view_only=True)) as ws:
            assert ws.receive_bytes() == RFB_GREETING
            ws.send_bytes(b"k")  # a normal console would send it straight back
            ws.receive_bytes()
        assert e.value.code == 4408  # idle: nothing, not b"k", came back
    finally:
        MockConsole.supports_view_only = False


# ── What reaches a log or a close frame ──────────────────────────────────────


def test_a_close_reason_is_cut_to_123_bytes_of_whole_characters():
    assert relay_router._fit("short") == "short"
    cut = relay_router._fit("é" * 100)  # 200 bytes
    assert len(cut.encode()) <= relay_router.MAX_REASON_BYTES and set(cut) == {"é"}
    assert len(relay_router._fit("日" * 100).encode()) <= relay_router.MAX_REASON_BYTES


def test_a_long_non_ascii_refusal_still_reaches_the_browser_with_its_code(client, world):
    MockConsole.dial_error = "Ça ne marche pas: " + "é" * 200
    code, reason = _closed_with(client, world.grant(world.ann))
    assert code == 4502 and 0 < len(reason.encode()) <= relay_router.MAX_REASON_BYTES


def test_the_grant_in_the_url_is_redacted_where_requests_are_logged():
    from app.middleware import RedactSecretPaths, redact_path

    assert redact_path("/ws/console/abcDEF-123_x") == "/ws/console/<redacted>"
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", "/ws/console/SECRETTOKEN", "1.1", 101), None)
    RedactSecretPaths().filter(record)
    assert "SECRETTOKEN" not in record.getMessage() and "<redacted>" in record.getMessage()


def test_the_upstream_handshake_is_kept_out_of_debug_logs():
    """The library prints the request line (the ticket) at DEBUG; our own logger stays at WARNING."""
    assert relay_router._upstream_logger.level == logging.WARNING


def test_a_dial_that_cannot_be_submitted_does_not_leave_the_user_counted(client, world, monkeypatch):
    class Broken:
        def submit(self, *a, **k):
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(relay_router, "_dial_pool", Broken())
    with pytest.raises((RuntimeError, WebSocketDisconnect)), _open(client, world.grant(world.ann)) as ws:  # the server error surfaces
        ws.receive_bytes()
    assert not relay_router._dials_in_flight


def test_a_refusal_for_a_dial_still_out_does_not_spend_the_minutes_attempts(client, world, monkeypatch):
    monkeypatch.setattr(relay_router, "DIAL_SECONDS", 0.2)
    release = threading.Event()
    real = MockConsole.dial
    monkeypatch.setattr(MockConsole, "dial", lambda self, grant, *, db=None, rng=None: (release.wait(5), real(self, grant, db=db, rng=rng))[1])
    spent = []
    original = service.dial_allowed
    monkeypatch.setattr(service, "dial_allowed", lambda uid: (spent.append(uid), original(uid))[1])
    try:
        assert _closed_with(client, world.grant(world.ann))[0] == 4504
        before = len(spent)
        for _ in range(3):
            assert _closed_with(client, world.grant(world.ann, vm_name="b"))[0] == 4429
        assert len(spent) == before  # the refusals cost no attempts
    finally:
        release.set()
    assert _wait_for(lambda: not relay_router._dials_in_flight)


def test_the_hypervisor_is_dialled_without_a_proxy(monkeypatch):
    import asyncio

    from app.console_backends import DialTarget

    seen = {}

    async def fake_connect(url, **kw):
        seen.update(kw)
        return object()

    import websockets.asyncio.client as ws_client

    monkeypatch.setattr(ws_client, "connect", fake_connect)
    asyncio.run(relay_router._upstream_connect(DialTarget(url="ws://127.0.0.1:1/ticket/x")))
    assert seen["proxy"] is None and seen["logger"] is relay_router._upstream_logger
